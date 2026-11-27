from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select, tuple_

from memfxxk.db import jobs, memory_blocks, records, transaction
from memfxxk.domain import Principal, RequestContext, Scope
from memfxxk.domain.context import BuildContextRequest, ContextBundle, SearchFilters
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.reflect import (
    OpinionChange,
    OpinionSuggestion,
    ReflectCitation,
    ReflectRequest,
    ReflectResult,
)
from memfxxk.domain.retrieval import MemoryType, SearchRequest
from memfxxk.ids import derived_id
from memfxxk.jobs.queue import Job, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2 import blocks
from memfxxk.m2.storage import derived_record
from memfxxk.manage.common import lock_user_operations
from memfxxk.prompts import opinion_update, reflect
from memfxxk.retrieval import NoopReranker, marks_for, search
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService


@dataclass(frozen=True, slots=True)
class Evidence:
    citation: ReflectCitation
    generation: int | None
    sources: tuple[UUID, ...]


def _layer(
    service: BoundService, query: str, request: ReflectRequest, layer: int, left: int
) -> ContextBundle:
    kinds: list[MemoryType] = ["summary", "note", "opinion"] if layer == 1 else ["turn", "fact"]
    return service.build_context(
        BuildContextRequest(
            query=query,
            max_tokens=left,
            sections=["documents"] if layer == 0 else ["evidence"],
            search_filters=SearchFilters(types=kinds, query_time=request.query_time),
        )
    )


def _evidence(service: BoundService, bundle: ContextBundle) -> dict[UUID, Evidence]:
    references = [item for section in bundle.sections for item in section.items]
    result = {}
    with service.engine.connect() as conn:
        versions = {}
        # Index by version so a stale context reference cannot silently cite another revision.
        for row in conn.execute(
            select(records).where(
                visible_filter(records, service.ctx),
                records.c.status == "active",
                tuple_(records.c.memory_id, records.c.version).in_(
                    [(UUID(ref.ref_id), ref.version) for ref in references if ref.kind == "memory"]
                ),
            )
        ).mappings():
            versions[row["memory_id"], row["version"]] = row
        candidates = (
            {item.id: item for item in blocks.list_visible(conn, service.ctx)}
            if any(ref.kind == "block" for ref in references)
            else {}
        )
        for ref in references:
            if ref.kind == "memory":
                key = UUID(ref.ref_id)
                row = versions.get((key, ref.version))
                if row is not None:
                    result[key] = Evidence(
                        ReflectCitation(
                            id=key, kind="memory", record_type=row["type"], version=row["version"]
                        ),
                        row["generation"],
                        tuple(row["source_event_ids"]),
                    )
            if ref.kind == "block":
                key = UUID(ref.ref_id)
                block = candidates.get(key)
                if block is not None and block.revision == ref.revision:
                    result[key] = Evidence(
                        ReflectCitation(
                            id=key, kind="block", record_type="memory_block", version=block.revision
                        ),
                        None,
                        tuple(block.source_event_ids),
                    )
    return result


def _check(
    conn: Connection,
    ctx: Scope,
    supplied: dict[UUID, Evidence],
    *,
    lock: bool = False,
) -> dict[UUID, Evidence]:
    memory = {
        (item.citation.id, item.citation.version)
        for item in supplied.values()
        if item.citation.kind == "memory"
    }
    selected = (
        select(records.c.memory_id, records.c.version, records.c.generation)
        .where(
            visible_filter(records, ctx),
            records.c.status == "active",
            records.c.is_current,
            tuple_(records.c.memory_id, records.c.version).in_(memory),
        )
        .order_by(records.c.id)
    )
    current = {
        ident: (version, generation)
        for ident, version, generation in conn.execute(
            selected.with_for_update() if lock else selected
        )
    }
    query = (
        select(memory_blocks.c.id, memory_blocks.c.head_revision)
        .where(
            visible_filter(memory_blocks, ctx),
            memory_blocks.c.id.in_(
                {key for key, item in supplied.items() if item.citation.kind == "block"}
            ),
        )
        .order_by(memory_blocks.c.id)
    )
    revisions = dict(conn.execute(query.with_for_update() if lock else query).all())
    return {
        key: item
        for key, item in supplied.items()
        if (
            item.citation.kind == "memory"
            and current.get(key) == (item.citation.version, item.generation)
        )
        or (item.citation.kind == "block" and revisions.get(key) == item.citation.version)
    }


def run(service: BoundService, request: ReflectRequest) -> ReflectResult:
    if service.settings.strategies.reflect != "hindsight":
        raise ServiceError(409, "reflect_disabled", "Enable the Hindsight reflect strategy first")
    if request.store_opinions:
        service.require_write()
    available: dict[UUID, Evidence] = {}
    context: list[str] = []
    bundles = []
    left = request.max_tokens
    query = request.query
    layer = 0
    answer = None
    turns = 0
    runner = TaskRunner.for_service(service, "reflect")
    for index in range(request.max_rounds):
        last = index == request.max_rounds - 1
        while left:
            next_layer = min(layer, 2)
            bundle = _layer(service, query, request, next_layer, left)
            bundles.append(bundle.bundle_id)
            layer += 1
            if bundle.rendered:
                context.append(bundle.rendered)
                available.update(_evidence(service, bundle))
                left -= bundle.total_tokens
                if not last:
                    break
            if layer >= 3:
                break
        dependencies = {source for item in available.values() for source in item.sources}
        response = runner.complete(
            "reflect",
            service.ctx,
            [
                {"role": "system", "content": reflect.PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": request.query,
                            "disposition": request.disposition.model_dump(),
                            "query_time": request.query_time.isoformat()
                            if request.query_time
                            else None,
                            "evidence": context,
                            "may_recall_more": not last and left > 0,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            schema=reflect.SCHEMA,
            prompt_version=reflect.VERSION,
            source_event_ids=sorted(dependencies),
        ).parsed
        turns += 1
        if isinstance(response, reflect.Answer):
            answer = response
            break
        if not isinstance(response, reflect.Recall) or not left:
            break
        query = response.query
    if answer is None:
        answer = reflect.Answer(
            action="answer", answer="I could not verify an answer from the available memory."
        )
    with transaction(service.engine) as conn:
        if request.store_opinions:
            lock_user_operations(conn, service.ctx)
        current = _check(conn, service.ctx, available, lock=request.store_opinions)
        cited = list(dict.fromkeys(answer.citations))
        proposals = [
            item
            for item in answer.opinions
            if set(item.citations) <= current.keys()
            and any(current[key].citation.kind == "memory" for key in item.citations)
        ]
        changes = (
            _store(conn, service.ctx, proposals, current)
            if request.store_opinions and current == available
            else []
        )
    accepted = [current[key].citation for key in cited if key in current]
    if cited and not accepted:
        answer.answer = "I could not verify an answer from the available memory."
    return ReflectResult(
        answer=answer.answer,
        citations=accepted,
        rejected_citations=[key for key in cited if key not in current],
        proposals=proposals,
        opinion_changes=changes,
        bundle_ids=bundles,
        rounds=turns,
    )


def _store(
    conn: Connection,
    ctx: RequestContext,
    suggestions: list[OpinionSuggestion],
    supplied: dict[UUID, Evidence],
) -> list[OpinionChange]:
    scope = Scope.model_validate(ctx.model_dump())
    sources = sorted({source for item in supplied.values() for source in item.sources})
    if not suggestions or not blocks.available_sources(conn, scope, set(sources)):
        return []
    changes = []
    for item in suggestions:
        key = " ".join(item.key.casefold().split())
        identity = derived_id(
            "opinion", *[getattr(scope, field) or "" for field in Scope.model_fields], key
        )
        exists = conn.scalar(
            select(records.c.id)
            .where(
                visible_filter(records, ctx),
                records.c.memory_id == identity,
            )
            .limit(1)
        )
        if exists:
            continue
        derived_record(
            conn,
            ctx,
            scope,
            memory_id=identity,
            version=1,
            kind="opinion",
            content=item.content,
            sources=sources,
            event_time=datetime.now(UTC),
            metadata={
                "strategy": "hindsight",
                "prompt_version": reflect.VERSION,
                "key": key,
                "confidence": item.confidence,
                "citations": [str(i) for i in item.citations],
            },
        )
        changes.append(
            OpinionChange(
                memory_id=identity,
                version=1,
                confidence=item.confidence,
                reason="created",
            )
        )
    return changes


def refresh(service: MemoryService, job: Job) -> None:
    ctx = RequestContext(**job.payload["scope"], actor={"type": "system"})
    scope = Scope.model_validate(ctx.model_dump())
    target = select(records).where(
        visible_filter(records, ctx),
        records.c.id == UUID(job.payload["record_id"]),
        records.c.generation == job.payload["generation"],
        records.c.is_current,
        records.c.status == "active",
        records.c.type.in_(("turn", "fact")),
    )
    bound = service.for_request(Principal(org_id=ctx.org_id, role="admin"), ctx)
    with service.engine.connect() as conn:
        trigger = conn.execute(target).mappings().one_or_none()
        if trigger is None:
            return
        exists = conn.scalar(
            select(records.c.id)
            .where(
                visible_filter(records, ctx),
                records.c.is_current,
                records.c.status == "active",
                records.c.type == "opinion",
            )
            .limit(1)
        )
        if not exists:
            return
        hits = search(
            bound,
            SearchRequest(query=trigger["content"][:4000], types=["opinion"], k=8),
            reranker=NoopReranker(),
        ).hits
        limits = (
            visible_filter(records, ctx),
            records.c.is_current,
            records.c.status == "active",
            records.c.type == "opinion",
            ~records.c.confirmed,
            records.c.origin == "auto_extract",
        )
        opinions = {
            row["memory_id"]: row
            for row in conn.execute(
                select(records).where(
                    *limits,
                    *(
                        records.c[field].is_not_distinct_from(getattr(scope, field))
                        for field in Scope.model_fields
                    ),
                    records.c.memory_id.in_([hit.memory_id for hit in hits]),
                )
            ).mappings()
            if not set(trigger["source_event_ids"]) & set(row["source_event_ids"])
        }
    if not opinions:
        return
    dependencies = set(trigger["source_event_ids"]) | {
        key for row in opinions.values() for key in row["source_event_ids"]
    }
    decision = (
        TaskRunner.for_service(service, "opinion_update")
        .complete(
            "opinion_update",
            ctx,
            [
                {"role": "system", "content": opinion_update.PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "new_evidence": trigger["content"],
                            "opinions": [
                                {
                                    "id": str(i),
                                    "content": row["content"],
                                    "confidence": row["metadata"]["confidence"],
                                }
                                for i, row in opinions.items()
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            schema=opinion_update.SCHEMA,
            prompt_version=opinion_update.VERSION,
            source_event_ids=sorted(dependencies),
            job_id=job.id,
        )
        .parsed
    )
    if decision is None:
        return
    weights = {"strengthens": 1, "weakens": -1, "contradicts": -2, "irrelevant": 0}
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        if (
            conn.execute(target.with_for_update()).first() is None
            or not blocks.available_sources(conn, scope, dependencies)
            or conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None
        ):
            return
        active = {
            row["memory_id"]: row
            for row in conn.execute(
                select(records)
                .where(
                    *limits,
                    records.c.memory_id.in_(opinions),
                )
                .order_by(records.c.id)
                .with_for_update()
            ).mappings()
        }
        marked = marks_for(conn, ctx, list(active))
        for item in {outcome.id: outcome for outcome in decision.assessments}.values():
            prior = opinions.get(item.id)
            row = active.get(item.id)
            if (
                row is None
                or prior is None
                or marked.get(item.id)
                or not weights[item.effect]
                or row["generation"] != prior["generation"]
                or row["id"] != prior["id"]
            ):
                continue
            previous = row["metadata"]["confidence"]
            confidence = min(1.0, max(0.0, previous + weights[item.effect] * 0.1))
            if confidence == previous:
                continue
            derived_record(
                conn,
                ctx,
                scope,
                memory_id=item.id,
                version=row["version"] + 1,
                kind="opinion",
                content=row["content"],
                sources=sorted(dependencies),
                event_time=trigger["event_time"],
                metadata={
                    **row["metadata"],
                    "confidence": confidence,
                    "last_effect": item.effect,
                    "prompt_version": opinion_update.VERSION,
                },
            )
