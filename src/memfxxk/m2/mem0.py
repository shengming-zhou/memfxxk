from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from math import sqrt
from typing import TYPE_CHECKING, Any, Literal
from unicodedata import normalize
from uuid import UUID

from sqlalchemy import Connection, Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import (
    events,
    fact_proposals,
    jobs,
    memory_marks,
    memory_relations,
    record_embeddings,
    records,
    transaction,
)
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.facts import FactCandidate
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.embedding import CachedEmbedder
from memfxxk.ids import derived_id
from memfxxk.jobs.queue import Job, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2.facts import _source_query
from memfxxk.m2.storage import derived_record
from memfxxk.manage.common import ManagementError, lock_user_operations
from memfxxk.prompts import extract, reconcile
from memfxxk.retrieval.channels import PgVectorIndex, search_filters
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import MemoryService

logger = logging.getLogger(__name__)
_NEARBY_LIMIT = 10
_ENTITY_MATCH = 0.95


@dataclass(frozen=True, slots=True)
class Turn:
    ctx: RequestContext
    current: list[RowMapping]
    recent: list[RowMapping]
    prior_summary: RowMapping | None

    @property
    def observed_at(self) -> datetime:
        return self.current[-1]["event_time"]


@dataclass(frozen=True, slots=True)
class Write:
    operation: Literal["ADD", "UPDATE", "DELETE"]
    fact: extract.ExtractedFact
    content: str
    target: RowMapping | None = None
    attributed_to: str | None = None
    links: list[RowMapping] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    aliases: dict[str, tuple[str, RowMapping]] = field(default_factory=dict)


def _turn_query(job: Job) -> Select:
    statement = select(records).where(
        records.c.id == UUID(job.payload["record_id"]),
        records.c.generation == job.payload["generation"],
        records.c.type == "turn",
        records.c.is_current,
        records.c.status == "active",
    )
    if "scope" in job.payload:
        statement = statement.where(
            visible_filter(records, Scope.model_validate(job.payload["scope"]))
        )
    return statement


def _load_turn(service: MemoryService, job: Job) -> Turn | None:
    with service.engine.connect() as conn:
        record = conn.execute(_turn_query(job)).mappings().one_or_none()
        if record is None:
            return None
        scope = Scope.model_validate(dict(record))
        ctx = RequestContext(**scope.model_dump(), actor={"type": "system"})
        result = list(conn.execute(_source_query(scope, record["source_event_ids"])).mappings())
        source = [row for row in result if row["kind"] == "message" and row["content"]]
        if (
            len(result) != len(record["source_event_ids"])
            or len(source) < 2
            or source[-1]["role"] != "assistant"
            or not any(row["role"] == "user" for row in source)
        ):
            return None
        if not record["source_pos"] or record["source_pos"].get("seq_from") is None:
            return None
        boundary = record["source_pos"]["seq_from"]
        previous = (
            conn.execute(
                select(events)
                .where(
                    visible_filter(events, ctx),
                    events.c.session_id == record["session_id"],
                    events.c.session_seq < boundary,
                    events.c.kind == "message",
                    events.c.role.in_(("user", "assistant")),
                    ~events.c.redacted,
                )
                .order_by(events.c.session_seq.desc())
                .limit(10)
            )
            .mappings()
            .all()
        )
        summary = (
            conn.execute(
                select(records.c.content, records.c.source_event_ids).where(
                    visible_filter(records, ctx),
                    records.c.session_id == record["session_id"],
                    records.c.type == "summary",
                    records.c.is_current,
                    records.c.status == "active",
                    records.c.source_pos["seq_to"].as_integer() < boundary,
                )
            )
            .mappings()
            .one_or_none()
        )
    return Turn(ctx, source, list(reversed(previous)), summary)


def _event_data(row: RowMapping) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "event_time": row["event_time"].isoformat(),
    }


def _memory_data(index: int, row: RowMapping) -> dict[str, Any]:
    return {
        "id": index,
        "text": row["content"],
        "valid_from": (row["valid_from"] or row["event_time"]).isoformat(),
    }


def _nearby(
    service: MemoryService, turn: Turn, texts: list[str], embedder: CachedEmbedder
) -> list[list[RowMapping]]:
    if not texts:
        return []
    filters = search_filters(
        turn.ctx, SearchRequest(query="", types=["fact"], as_of_valid=turn.observed_at)
    )
    index = PgVectorIndex(embedder.model, embedder.dim, service.settings.search.hnsw_ef_search)
    vectors = embedder.embed(texts)
    with service.engine.connect() as conn:
        index.enable_iterative_scan(conn)
        indexed = [
            index.search(conn, vector, [*filters, records.c.is_current], _NEARBY_LIMIT)
            for vector in vectors
        ]
        ids = {hit.id for hits in indexed for hit in hits}
        indexed_rows = conn.execute(
            select(records).where(records.c.id.in_(ids), records.c.is_current, *filters)
        ).mappings()
        by_id = {row["id"]: row for row in indexed_rows}
        embedding_exists = (
            select(record_embeddings.c.record_id)
            .where(
                record_embeddings.c.record_id == records.c.id,
                record_embeddings.c.model == embedder.model,
                record_embeddings.c.dim == embedder.dim,
            )
            .exists()
        )
        pending = list(
            conn.execute(
                select(records).where(*filters, records.c.is_current, ~embedding_exists)
            ).mappings()
        )
    # Recently written facts are valid before the async embedding job finishes.
    pending_vectors = embedder.embed([row["content"] for row in pending]) if pending else []
    matches = []
    for vector, hits in zip(vectors, indexed, strict=True):
        ranked = [(hit.score, by_id[hit.id]) for hit in hits if hit.id in by_id]
        ranked.extend(
            (_cosine(vector, pending_vector), row)
            for row, pending_vector in zip(pending, pending_vectors, strict=True)
        )
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        matches.append([row for _, row in ranked[:_NEARBY_LIMIT]])
    return matches


def _messages(prompt: str, data: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]


def _debug_sources(turn: Turn, nearby: Sequence[RowMapping] = ()) -> list[UUID]:
    return [
        *(row["id"] for row in [*turn.current, *turn.recent]),
        *(key for row in nearby for key in row["source_event_ids"]),
        *(turn.prior_summary["source_event_ids"] if turn.prior_summary else ()),
    ]


def _current_fact(fact: extract.ExtractedFact, turn: Turn) -> bool:
    return bool(fact.content.strip()) and set(fact.source_event_ids) <= {
        row["id"] for row in turn.current
    }


def _classic(service: MemoryService, job: Job, turn: Turn, embedder: CachedEmbedder) -> list[Write]:
    extraction = (
        TaskRunner.for_service(service, "extract")
        .complete(
            "extract",
            turn.ctx,
            _messages(
                extract.CLASSIC_PROMPT,
                {
                    "prior_summary": turn.prior_summary["content"] if turn.prior_summary else "",
                    "recent_messages": [_event_data(row) for row in turn.recent],
                    "new_turn": [_event_data(row) for row in turn.current],
                },
            ),
            schema=extract.CLASSIC_SCHEMA,
            prompt_version=extract.CLASSIC_VERSION,
            job_id=job.id,
            source_event_ids=_debug_sources(turn),
        )
        .parsed
    )
    if extraction is None:
        return []
    facts = [fact for fact in extraction.facts if _current_fact(fact, turn)]
    if not facts:
        return []
    matches = _nearby(service, turn, [fact.content for fact in facts], embedder)
    rows = {row["memory_id"]: row for found in matches for row in found}
    ids = {key: index for index, key in enumerate(rows)}
    existing = list(rows.values())
    linked_ids = [{ids[row["memory_id"]] for row in found} for found in matches]
    reconciliation = (
        TaskRunner.for_service(service, "reconcile")
        .complete(
            "reconcile",
            turn.ctx,
            _messages(
                reconcile.PROMPT,
                {
                    "candidates": [
                        {
                            "candidate_id": index,
                            "text": fact.content,
                            "effective": fact.valid_from.isoformat() if fact.valid_from else None,
                            "related_ids": sorted(linked_ids[index]),
                        }
                        for index, fact in enumerate(facts)
                    ],
                    "existing_memories": [
                        _memory_data(index, row) for index, row in enumerate(existing)
                    ],
                },
            ),
            schema=reconcile.SCHEMA,
            prompt_version=reconcile.VERSION,
            job_id=job.id,
            source_event_ids=_debug_sources(turn, existing),
        )
        .parsed
    )
    if reconciliation is None:
        return []
    writes = []
    seen = set()
    for action in reconciliation.memory:
        candidate_id = action.candidate_id
        if not 0 <= candidate_id < len(facts) or candidate_id in seen:
            continue
        seen.add(candidate_id)
        if action.event == "NONE":
            continue
        fact = facts[candidate_id]
        if action.event == "ADD":
            writes.append(Write("ADD", fact, fact.content.strip()))
            continue
        if action.id is None or action.id not in linked_ids[candidate_id]:
            logger.warning("mem0 discarded an action with an out-of-range existing memory ID")
            continue
        target = existing[action.id]
        if action.event == "UPDATE":
            if action.old_memory != target["content"] or not (action.text or "").strip():
                continue
            writes.append(Write("UPDATE", fact, action.text.strip(), target))
        elif action.event == "DELETE":
            writes.append(Write("DELETE", fact, target["content"], target))
    return writes


def _name(text: str) -> str:
    return " ".join(normalize("NFKC", text).casefold().split())


def _cosine(a: list[float], b: list[float]) -> float:
    denom = sqrt(sum(value * value for value in a) * sum(value * value for value in b))
    return sum(left * right for left, right in zip(a, b, strict=True)) / denom if denom else 0


def _entities(
    names: list[str], known: dict[str, RowMapping], embedder: CachedEmbedder
) -> tuple[list[str], dict[str, tuple[str, RowMapping]]]:
    names = sorted({label for raw in names if 0 < len(label := _name(raw)) <= 80})
    to_compare = sorted(set(names) - set(known))
    if not known or not to_compare:
        return names, {}
    vectors = dict(zip([*known, *to_compare], embedder.embed([*known, *to_compare]), strict=True))
    aliases = {}
    for label in to_compare:
        score, match = max((_cosine(vectors[label], vectors[other]), other) for other in known)
        if score >= _ENTITY_MATCH:
            aliases[label] = (match, known[match])
    return names, aliases


def _additive(
    service: MemoryService, job: Job, turn: Turn, embedder: CachedEmbedder
) -> list[Write]:
    [existing] = _nearby(
        service, turn, ["\n".join(row["content"] for row in turn.current)], embedder
    )
    response = (
        TaskRunner.for_service(service, "extract")
        .complete(
            "extract",
            turn.ctx,
            _messages(
                extract.ADDITIVE_PROMPT,
                {
                    "recent_messages": [_event_data(row) for row in turn.recent],
                    "existing_memories": [
                        _memory_data(index, row) for index, row in enumerate(existing)
                    ],
                    "new_turn": [_event_data(row) for row in turn.current],
                },
            ),
            schema=extract.ADDITIVE_SCHEMA,
            prompt_version=extract.ADDITIVE_VERSION,
            job_id=job.id,
            source_event_ids=_debug_sources(turn, existing),
        )
        .parsed
    )
    if response is None:
        return []
    known = {
        _name(label): row
        for row in reversed(existing)
        for label in row["metadata"].get("entities", [])
    }
    event_roles = {row["id"]: row["role"] for row in turn.current}
    writes = []
    for fact in response.memory:
        if not _current_fact(fact, turn) or not any(
            event_roles[key] == fact.attributed_to for key in fact.source_event_ids
        ):
            continue
        entities, aliases = _entities(fact.entities, known, embedder)
        writes.append(
            Write(
                "ADD",
                fact,
                fact.content.strip(),
                attributed_to=fact.attributed_to,
                links=[
                    existing[index]
                    for index in dict.fromkeys(fact.linked_memory_ids)
                    if 0 <= index < len(existing)
                ],
                entities=entities,
                aliases=aliases,
            )
        )
    return writes


def _target_query(ctx: Scope, target: RowMapping) -> Select:
    return select(records).where(
        visible_filter(records, ctx),
        records.c.id == target["id"],
        records.c.generation == target["generation"],
        records.c.is_current,
        records.c.status == "active",
    )


def _requires_proposal(conn: Connection, ctx: RequestContext, target: RowMapping) -> bool:
    if target["confirmed"] or target["origin"] == "user_explicit":
        return True
    return (
        conn.scalar(
            select(memory_marks.c.id)
            .where(
                visible_filter(memory_marks, ctx), memory_marks.c.memory_id == target["memory_id"]
            )
            .limit(1)
        )
        is not None
    )


def _metadata(strategy: str) -> dict[str, Any]:
    versions = (
        [extract.ADDITIVE_VERSION]
        if strategy == "mem0_additive"
        else [extract.CLASSIC_VERSION, reconcile.VERSION]
    )
    return {"strategy": strategy, "prompt_versions": versions}


def _content_hash(content: str) -> str:
    return sha256(" ".join(normalize("NFKC", content).split()).encode()).hexdigest()


def _record_update(
    conn: Connection,
    ctx: RequestContext,
    target: RowMapping,
    fact: FactCandidate,
    observed: datetime,
    operation: Literal["UPDATE", "DELETE"],
    *,
    confirmed: bool = False,
) -> int:
    version = target["version"] + 1
    tombstone = operation == "DELETE"
    metadata = {
        **target["metadata"],
        **_metadata("mem0_classic"),
        "content_sha256": _content_hash(fact.content),
    }
    if tombstone:
        metadata["temporal_tombstone"] = True
    sources = list(dict.fromkeys([*target["source_event_ids"], *fact.source_event_ids]))
    effective = fact.valid_from or observed
    derived_record(
        conn,
        ctx,
        Scope.model_validate(dict(target)),
        memory_id=target["memory_id"],
        version=version,
        kind="fact",
        content=fact.content,
        event_time=observed,
        valid_from=effective,
        valid_to=effective if tombstone else None,
        status="inactive" if tombstone else "active",
        sources=sources,
        confirmed=confirmed,
        metadata=metadata,
    )
    return version


def accept_action(
    conn: Connection,
    ctx: RequestContext,
    proposal: RowMapping,
    fact: FactCandidate,
    timestamp: datetime,
) -> int:
    target = (
        conn.execute(
            select(records)
            .where(
                visible_filter(records, ctx),
                records.c.memory_id == proposal["memory_id"],
                records.c.is_current,
                records.c.status == "active",
                records.c.type == "fact",
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if target is None or target["version"] != proposal["base_version"]:
        raise ManagementError(409, "stale_fact_proposal", "The target fact has changed")
    return _record_update(
        conn,
        ctx,
        target,
        fact,
        timestamp,
        "DELETE" if proposal["operation"] == "delete" else "UPDATE",
        confirmed=True,
    )


def _proposal(
    conn: Connection, ctx: RequestContext, target: RowMapping, fact: FactCandidate, operation: str
) -> None:
    identity = derived_id(
        "mem0-review",
        str(target["memory_id"]),
        str(target["version"]),
        operation,
        fact.content,
        fact.valid_from.isoformat() if fact.valid_from else "",
    )
    conn.execute(
        insert(fact_proposals)
        .values(
            id=identity,
            **ctx.model_dump(exclude={"actor"}),
            memory_id=target["memory_id"],
            base_version=target["version"],
            operation=operation.lower(),
            source_event_ids=fact.source_event_ids,
            fact=fact.model_dump(mode="json"),
        )
        .on_conflict_do_nothing()
    )


def _add(
    conn: Connection, ctx: RequestContext, strategy: str, write: Write, observed: datetime
) -> None:
    digest = _content_hash(write.content)
    scope = Scope.model_validate(ctx.model_dump())
    exact_scope = [
        records.c[key].is_not_distinct_from(value) for key, value in scope.model_dump().items()
    ]
    if (
        conn.scalar(
            select(records.c.id)
            .where(
                visible_filter(records, ctx),
                *exact_scope,
                records.c.type == "fact",
                records.c.is_current,
                records.c.status == "active",
                records.c.metadata["content_sha256"].as_string() == digest,
            )
            .limit(1)
        )
        is not None
    ):
        return
    memory_id = derived_id("mem0-fact", json.dumps(scope.model_dump(), sort_keys=True), digest)
    existing = conn.execute(
        select(records.c.status, records.c.confirmed, records.c.metadata).where(
            records.c.memory_id == memory_id, visible_filter(records, ctx), records.c.is_current
        )
    ).one_or_none()
    if existing is not None:
        if existing.status == "inactive" and (
            existing.confirmed or not existing.metadata.get("temporal_tombstone")
        ):
            return
        # A previously auto-retracted or changed fact can be asserted again.
        memory_id = derived_id(
            "mem0-fact-reasserted", str(memory_id), str(write.fact.source_event_ids[0])
        )
    sources = list(write.fact.source_event_ids)
    labels = set(write.entities)
    for label, (canonical, target) in write.aliases.items():
        if conn.execute(_target_query(ctx, target).with_for_update()).first() is None:
            continue
        labels.remove(label)
        labels.add(canonical)
        sources.extend(target["source_event_ids"])
    metadata = {**_metadata(strategy), "content_sha256": digest, "entities": sorted(labels)}
    if write.attributed_to:
        metadata["attributed_to"] = write.attributed_to
    derived_record(
        conn,
        ctx,
        scope,
        memory_id=memory_id,
        version=1,
        kind="fact",
        content=write.content,
        event_time=observed,
        valid_from=write.fact.valid_from or observed,
        sources=list(dict.fromkeys(sources)),
        metadata=metadata,
    )
    for target in write.links:
        if conn.execute(_target_query(ctx, target).with_for_update()).first() is None:
            continue
        conn.execute(
            insert(memory_relations)
            .values(
                id=derived_id("mem0-link", str(memory_id), str(target["memory_id"])),
                from_memory_id=memory_id,
                to_memory_id=target["memory_id"],
                purpose="mem0_additive",
                org_id=ctx.org_id,
                created_by=ctx.actor.model_dump(),
                source_event_id=write.fact.source_event_ids[0],
            )
            .on_conflict_do_nothing()
        )


def process(service: MemoryService, job: Job, embedder: CachedEmbedder) -> None:
    turn = _load_turn(service, job)
    if turn is None:
        return
    strategy = service.settings.fact_strategy
    writes = (
        _classic(service, job, turn, embedder)
        if strategy == "mem0_classic"
        else _additive(service, job, turn, embedder)
    )
    if not writes:
        return
    with transaction(service.engine) as conn:
        lock_user_operations(conn, turn.ctx)
        if (
            conn.execute(
                _turn_query(job).where(visible_filter(records, turn.ctx)).with_for_update()
            ).first()
            is None
        ):
            return
        if conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None:
            return
        for write in writes:
            if write.operation == "ADD":
                _add(conn, turn.ctx, strategy, write, turn.observed_at)
                continue
            assert write.target is not None
            target = (
                conn.execute(_target_query(turn.ctx, write.target).with_for_update())
                .mappings()
                .one_or_none()
            )
            if target is None or Scope.model_validate(dict(target)) != Scope.model_validate(
                turn.ctx.model_dump()
            ):
                continue
            if write.operation == "UPDATE" and target["content"] == write.content:
                continue
            effective = write.fact.valid_from or turn.observed_at
            if effective < (target["valid_from"] or target["event_time"]):
                continue
            fact = FactCandidate(
                key=target["metadata"].get("fact_key") or str(target["memory_id"]),
                content=write.content,
                source_event_ids=list(dict.fromkeys(write.fact.source_event_ids)),
                valid_from=effective,
                entities=target["metadata"].get("entities") or [],
            )
            if _requires_proposal(conn, turn.ctx, target):
                _proposal(conn, turn.ctx, target, fact, write.operation)
            else:
                _record_update(conn, turn.ctx, target, fact, turn.observed_at, write.operation)
