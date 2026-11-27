from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import (
    amem_links,
    amem_revisions,
    events,
    jobs,
    memory_relations,
    records,
    transaction,
)
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.notes import NoteAttributes, NoteHistoryEntry, NoteProjection, RollbackNote
from memfxxk.embedding import CachedEmbedder
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound
from memfxxk.jobs.queue import Job, enqueue, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.llm.settings import Task
from memfxxk.manage.common import ManagementError, current_record, lock_user_operations, write_audit
from memfxxk.prompts import note_enrich, note_evolve, note_link
from memfxxk.retrieval.channels import Candidate
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService

ELIGIBLE = ("note", "fact", "lesson")
NEIGHBORS = 10


@dataclass(frozen=True, slots=True)
class Projection:
    attributes: NoteAttributes
    embedding: list[float]
    sources: set[UUID]
    call_id: UUID | None


def _record(record_id: UUID, generation: int) -> Select:
    return select(records).where(
        records.c.id == record_id,
        records.c.generation == generation,
        records.c.is_current,
        records.c.status == "active",
        records.c.type.in_(ELIGIBLE),
    )


def _eligible(service: MemoryService, row: RowMapping) -> bool:
    return (
        service.settings.strategies.amem_enrich_mem0_facts
        or row["type"] != "fact"
        or row["metadata"].get("strategy") not in ("mem0_classic", "mem0_additive")
    )


def queue_from_embeddings(service: MemoryService, embedded_jobs: list[Job]) -> None:
    with transaction(service.engine) as conn:
        for job in embedded_jobs:
            record_id = UUID(job.payload["record_id"])
            generation = job.payload["generation"]
            scope_data = (
                conn.execute(
                    _record(record_id, generation).with_only_columns(
                        *(records.c[key] for key in Scope.model_fields),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if scope_data is None:
                continue
            scope = Scope.model_validate(dict(scope_data))
            row = (
                conn.execute(_record(record_id, generation).where(visible_filter(records, scope)))
                .mappings()
                .one_or_none()
            )
            if row is not None and "amem" not in row["metadata"] and _eligible(service, row):
                enqueue(
                    conn,
                    "build_amem_note",
                    {
                        "record_id": str(record_id),
                        "generation": generation,
                        "scope": scope.model_dump(),
                    },
                    dedupe_key=f"amem:{record_id}:{generation}",
                )


def _sources(conn: Connection, ctx: Scope, record: RowMapping) -> set[UUID]:
    if record["source_event_ids"]:
        return set(record["source_event_ids"])
    explicit = conn.scalar(
        select(events.c.id)
        .where(
            visible_filter(events, ctx),
            ~events.c.redacted,
            events.c.kind == "user_op",
            events.c.payload["target"].as_string() == str(record["memory_id"]),
            events.c.payload["args"]["content"].as_string() == record["content"],
        )
        .order_by(events.c.recorded_at.desc(), events.c.id.desc())
        .limit(1)
    )
    return {explicit} if explicit else set()


def _available(conn: Connection, ctx: Scope, source_ids: set[UUID]) -> bool:
    return (
        bool(source_ids)
        and set(
            conn.scalars(
                select(events.c.id).where(
                    visible_filter(events, ctx), events.c.id.in_(source_ids), ~events.c.redacted
                )
            )
        )
        == source_ids
    )


def _projection(row: RowMapping) -> NoteProjection | None:
    note = row["metadata"].get("amem")
    return NoteProjection.model_validate(note) if note is not None else None


def _call[T](
    service: MemoryService,
    ctx: Scope,
    job: Job,
    task: Task,
    prompt: str,
    version: str,
    schema: TypeAdapter[T],
    data: dict[str, Any],
    sources: set[UUID],
) -> tuple[T, UUID]:
    outcome = TaskRunner.for_service(service, task).complete(
        task,
        ctx,
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        schema=schema,
        prompt_version=version,
        job_id=job.id,
        source_event_ids=sorted(sources),
    )
    assert outcome.parsed is not None
    return outcome.parsed, outcome.call_id


def _text(content: str, attributes: NoteAttributes) -> str:
    return "\n".join(
        [content, attributes.context, " ".join(attributes.keywords), " ".join(attributes.tags)]
    )


def _input(row: RowMapping, attributes: NoteAttributes | None = None) -> dict[str, Any]:
    return {
        "content": row["content"],
        "event_time": row["event_time"].isoformat(),
        "attributes": attributes.model_dump() if attributes else None,
    }


def _vector_search(ctx: Scope, model: str, vector: list[float]) -> Select:
    distance = amem_revisions.c.embedding.cosine_distance(vector)
    return (
        select(records.c.id, records.c.generation, distance.label("distance"))
        .join(amem_revisions, amem_revisions.c.record_id == records.c.id)
        .where(
            visible_filter(records, ctx),
            visible_filter(amem_revisions, ctx),
            records.c.metadata["amem"]["revision"].as_integer() == amem_revisions.c.revision,
            amem_revisions.c.embedding_model == model,
            func.vector_dims(amem_revisions.c.embedding) == len(vector),
            records.c.is_current,
        )
        .order_by(distance, records.c.id)
        .limit(NEIGHBORS)
    )


def search_notes(
    conn: Connection,
    ctx: Scope,
    model: str,
    vector: list[float],
    filters: list,
) -> list[Candidate]:
    return [
        Candidate(record_id, generation, 1 - distance)
        for record_id, generation, distance in conn.execute(
            _vector_search(ctx, model, vector).where(*filters)
        )
        if distance is not None
    ]


def _neighbors(
    conn: Connection,
    ctx: RequestContext,
    row: RowMapping,
    embedder: CachedEmbedder,
    vector: list[float],
) -> list[tuple[RowMapping, NoteProjection, set[UUID]]]:
    exact_scope = [
        records.c[key].is_not_distinct_from(getattr(ctx, key)) for key in Scope.model_fields
    ]
    candidates = search_notes(
        conn,
        ctx,
        embedder.model,
        vector,
        [records.c.id != row["id"], records.c.status == "active", *exact_scope],
    )
    by_id = {
        item["id"]: item
        for item in conn.execute(
            select(records).where(
                visible_filter(records, ctx), records.c.id.in_([item.id for item in candidates])
            )
        ).mappings()
    }
    result = []
    for entry in candidates:
        item = by_id.get(entry.id)
        if item is None or item["generation"] != entry.generation:
            continue
        attributes = _projection(item)
        if attributes is None:
            continue
        stored_sources = conn.scalar(
            select(amem_revisions.c.source_event_ids).where(
                visible_filter(amem_revisions, ctx),
                amem_revisions.c.record_id == item["id"],
                amem_revisions.c.revision == attributes.revision,
            )
        )
        if stored_sources:
            result.append((item, attributes, set(stored_sources)))
    return result


def _save(
    conn: Connection,
    ctx: RequestContext,
    row: RowMapping,
    value: Projection,
    model: str,
    operation: Literal["enrich", "evolve", "rollback"],
) -> NoteProjection:
    latest = conn.scalar(
        select(func.max(amem_revisions.c.revision)).where(
            visible_filter(amem_revisions, ctx), amem_revisions.c.record_id == row["id"]
        )
    )
    scope = Scope.model_validate(dict(row))
    attributes = value.attributes.model_dump(exclude={"revision"}, mode="json")
    provenance = {"strategy": "amem"}
    if operation in ("enrich", "evolve"):
        provenance["prompt_version"] = (
            note_enrich.VERSION if operation == "enrich" else note_evolve.VERSION
        )
    view = NoteProjection(
        **attributes, revision=max(latest or 0, row["metadata"].get("amem_seq", 0)) + 1
    )
    conn.execute(
        amem_revisions.insert().values(
            **scope.model_dump(),
            record_id=row["id"],
            revision=view.revision,
            attributes=attributes,
            embedding=value.embedding,
            embedding_model=model,
            source_event_ids=sorted(value.sources),
            operation=operation,
            llm_call_id=value.call_id,
            metadata=provenance,
        )
    )
    conn.execute(
        records.update()
        .where(visible_filter(records, ctx), records.c.id == row["id"])
        .values(
            metadata={
                **row["metadata"],
                "amem": view.model_dump(mode="json"),
                "amem_seq": view.revision,
                "amem_provenance": provenance,
            }
        )
    )
    return view


def _store_link(
    conn: Connection,
    ctx: RequestContext,
    left: RowMapping,
    right: RowMapping,
    sources: set[UUID],
    call_id: UUID,
) -> None:
    relation_id = derived_id("amem-link", str(left["memory_id"]), str(right["memory_id"]))
    inserted = conn.scalar(
        insert(memory_relations)
        .values(
            id=relation_id,
            org_id=ctx.org_id,
            from_memory_id=left["memory_id"],
            to_memory_id=right["memory_id"],
            purpose="auto_link",
            source_event_id=min(sources),
            created_by=ctx.actor.model_dump(),
        )
        .on_conflict_do_nothing()
        .returning(memory_relations.c.id)
    )
    if inserted is not None:
        conn.execute(
            amem_links.insert().values(
                **ctx.model_dump(exclude={"actor"}),
                relation_id=relation_id,
                source_event_ids=sorted(sources),
                llm_call_id=call_id,
            )
        )


def build(service: MemoryService, job: Job, embedder: CachedEmbedder) -> None:
    scope = Scope.model_validate(job.payload["scope"])
    ctx = RequestContext(**scope.model_dump(), actor={"type": "system"})
    statement = _record(UUID(job.payload["record_id"]), job.payload["generation"]).where(
        visible_filter(records, ctx)
    )
    with service.engine.connect() as conn:
        row = conn.execute(statement).mappings().one_or_none()
        if row is None or _projection(row) or not _eligible(service, row):
            return
        raw = _sources(conn, ctx, row)
        if not _available(conn, ctx, raw):
            return
    data = _input(row)
    analysis, enrich_id = _call(
        service,
        ctx,
        job,
        "note_enrich",
        note_enrich.PROMPT,
        note_enrich.VERSION,
        note_enrich.SCHEMA,
        data,
        raw,
    )
    [vector] = embedder.embed([_text(row["content"], analysis)])
    with service.engine.connect() as conn:
        nearby = _neighbors(conn, ctx, row, embedder, vector)
    payload = {
        "new_note": _input(row, analysis),
        "neighbors": [
            {"id": number, **_input(candidate, attrs)}
            for number, (candidate, attrs, _) in enumerate(nearby)
        ],
    }
    influence = raw | {key for _, _, selected in nearby for key in selected}
    linked: list[int] = []
    link_id = None
    if nearby:
        links, link_id = _call(
            service,
            ctx,
            job,
            "note_link",
            note_link.PROMPT,
            note_link.VERSION,
            note_link.SCHEMA,
            payload,
            influence,
        )
        linked = [
            number
            for number in dict.fromkeys(links.suggested_connections)
            if 0 <= number < len(nearby)
        ]
    changes = {}
    for number, (target, _, _) in enumerate(nearby):
        if target["confirmed"] or target["origin"] == "user_explicit":
            continue
        response, evolve_id = _call(
            service,
            ctx,
            job,
            "note_evolve",
            note_evolve.PROMPT,
            note_evolve.VERSION,
            note_evolve.SCHEMA,
            {**payload, "target_id": number},
            influence,
        )
        if (
            response.should_evolve
            and response.actions == ["update_neighbor"]
            and response.attributes
        ):
            [enhanced] = embedder.embed([_text(target["content"], response.attributes)])
            changes[number] = Projection(response.attributes, enhanced, influence, evolve_id)
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        current = conn.execute(statement.with_for_update()).mappings().one_or_none()
        if current is None or _projection(current) or not _available(conn, ctx, raw):
            return
        if conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None:
            return
        _save(
            conn,
            ctx,
            current,
            Projection(analysis, vector, raw, enrich_id),
            embedder.model,
            "enrich",
        )
        if not _available(conn, ctx, influence):
            return
        for number, (candidate, attrs, _) in enumerate(nearby):
            if number not in linked and number not in changes:
                continue
            target = (
                conn.execute(
                    _record(candidate["id"], candidate["generation"])
                    .where(visible_filter(records, ctx))
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if target is None or _projection(target) != attrs:
                continue
            if number in linked and link_id is not None:
                _store_link(conn, ctx, current, target, influence, link_id)
            if (
                number in changes
                and not target["confirmed"]
                and target["origin"] != "user_explicit"
            ):
                _save(conn, ctx, target, changes[number], embedder.model, "evolve")


def history(service: BoundService, memory_id: UUID, limit: int) -> list[NoteHistoryEntry]:
    with service.engine.connect() as conn:
        row = current_record(conn, service.ctx, memory_id)
        revisions = conn.execute(
            select(amem_revisions)
            .where(
                visible_filter(amem_revisions, service.ctx),
                amem_revisions.c.record_id == row["id"],
            )
            .order_by(amem_revisions.c.revision.desc())
            .limit(limit)
        ).mappings()
        return [NoteHistoryEntry.model_validate(item) for item in revisions]


def rollback(service: BoundService, memory_id: UUID, request: RollbackNote) -> NoteProjection:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        row = current_record(conn, ctx, memory_id, lock=True)
        service.require_resource_write(row["project_id"])
        current = _projection(row)
        if current is None:
            raise NotFound
        if current.revision != request.base_revision:
            raise ManagementError(409, "note_revision_conflict", "Note attributes have changed")
        earlier = (
            conn.execute(
                select(amem_revisions).where(
                    visible_filter(amem_revisions, ctx),
                    amem_revisions.c.record_id == row["id"],
                    amem_revisions.c.revision == request.target_revision,
                )
            )
            .mappings()
            .one_or_none()
        )
        if earlier is None or not _available(conn, ctx, set(earlier["source_event_ids"])):
            raise NotFound
        attrs = NoteAttributes.model_validate(earlier["attributes"])
        projection = Projection(
            attrs,
            [float(item) for item in earlier["embedding"]],
            set(earlier["source_event_ids"]),
            None,
        )
        adopted = _save(conn, ctx, row, projection, earlier["embedding_model"], "rollback")
        write_audit(
            conn,
            ctx,
            "rollback_note",
            memory_id,
            request.model_dump(),
            {"revision": adopted.revision},
        )
        return adopted


def forget_source(conn: Connection, org_id: str, event_id: UUID) -> None:
    links = select(amem_links.c.relation_id).where(
        amem_links.c.org_id == org_id, amem_links.c.source_event_ids.contains([event_id])
    )
    conn.execute(
        memory_relations.delete().where(
            memory_relations.c.org_id == org_id, memory_relations.c.id.in_(links)
        )
    )
    affected = set(
        conn.execute(
            amem_revisions.delete()
            .where(
                amem_revisions.c.org_id == org_id,
                amem_revisions.c.source_event_ids.contains([event_id]),
            )
            .returning(amem_revisions.c.record_id, amem_revisions.c.revision)
        ).all()
    )
    for record_id, revision in affected:
        row = (
            conn.execute(
                select(records)
                .where(
                    records.c.org_id == org_id,
                    records.c.id == record_id,
                    records.c.metadata["amem"]["revision"].as_integer() == revision,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            continue
        latest = (
            conn.execute(
                select(amem_revisions)
                .where(
                    amem_revisions.c.org_id == org_id,
                    amem_revisions.c.record_id == record_id,
                )
                .order_by(amem_revisions.c.revision.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        metadata = dict(row["metadata"])
        if latest is None:
            metadata.pop("amem", None)
            metadata.pop("amem_provenance", None)
        else:
            metadata["amem"] = {**latest["attributes"], "revision": latest["revision"]}
            metadata["amem_provenance"] = latest["metadata"]
        conn.execute(
            records.update()
            .where(records.c.org_id == org_id, records.c.id == record_id)
            .values(metadata=metadata)
        )
