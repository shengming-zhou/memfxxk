from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import exp, sqrt
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ColumnElement, Connection, Select, and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import jobs, memory_blocks, records, tier_pages, tier_segments, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.embedding import CachedEmbedder, make_embedding_provider
from memfxxk.ids import derived_id
from memfxxk.jobs.queue import Job, enqueue, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2 import blocks
from memfxxk.manage.common import lock_user_operations
from memfxxk.prompts import memoryos
from memfxxk.retrieval.channels import search_filters
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer

if TYPE_CHECKING:
    from memfxxk.domain.retrieval import SearchRequest
    from memfxxk.service import BoundService, MemoryService

DECAY_SECONDS = 10_000_000
RETRIEVE_SEGMENTS = 5
TOKENIZER = MixedTokenizer()


def heat(visits: int, pages: int, last_access: datetime, now: datetime) -> float:
    elapsed = max(0.0, (now - last_access).total_seconds())
    return visits + pages + exp(-elapsed / DECAY_SECONDS)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    norm = sqrt(sum(x * x for x in left) * sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / norm if norm else 0.0


def similarity(
    left: Sequence[float], right: Sequence[float], keywords: set[str], other: set[str]
) -> float:
    union = keywords | other
    return cosine(left, right) + (len(keywords & other) / len(union) if union else 0.0)


def _bank(scope: Scope) -> UUID:
    return derived_id("memoryos-bank", *(getattr(scope, key) or "" for key in Scope.model_fields))


def _page_bank(ctx: Scope, bank: UUID) -> Select:
    return (
        select(records, tier_pages.c.segment_id)
        .join(tier_pages, tier_pages.c.record_id == records.c.id)
        .where(
            visible_filter(records, ctx),
            tier_pages.c.bank_id == bank,
            tier_pages.c.generation == records.c.generation,
            records.c.is_current,
            records.c.status == "active",
        )
    )


def _segments(ctx: Scope) -> Select:
    return select(tier_segments).where(
        visible_filter(tier_segments, ctx), tier_segments.c.bank_id == _bank(ctx)
    )


def _heat(row: RowMapping, now: datetime) -> float:
    return heat(row["visits"], row["unpromoted_pages"], row["last_access"], now)


def _queue_summary(conn: Connection, segment: RowMapping) -> None:
    key, generation, epoch = segment["id"], segment["generation"], segment["promotion_epoch"]
    enqueue(
        conn,
        "tier_summarize",
        {
            "segment_id": str(key),
            "generation": generation,
            "epoch": epoch,
            "scope": Scope.model_validate(dict(segment)).model_dump(),
        },
        dedupe_key=f"memoryos:{key}:{generation}:{epoch}",
    )


def _evict(conn: Connection, ctx: Scope, segment_ids: set[UUID]) -> None:
    if not segment_ids:
        return
    scoped = select(tier_segments.c.id).where(
        visible_filter(tier_segments, ctx), tier_segments.c.id.in_(segment_ids)
    )
    conn.execute(
        tier_pages.update()
        .where(tier_pages.c.segment_id.in_(scoped))
        .values(segment_id=None, state="evicted")
    )
    conn.execute(tier_segments.delete().where(tier_segments.c.id.in_(scoped)))


def _assign(
    conn: Connection,
    ctx: RequestContext,
    old: RowMapping,
    vector: list[float],
    settings: MemoryService,
    now: datetime,
) -> None:
    tiers = settings.settings.tiers
    model = settings.settings.embedding.model
    available = list(conn.execute(_segments(ctx).with_for_update()).mappings())
    words = set(TOKENIZER.tokenize(old["content"]))
    ranked = sorted(
        (
            (similarity(vector, row["embedding"], words, set(row["keywords"])), row)
            for row in available
            if row["model"] == model
        ),
        key=lambda item: (-item[0], item[1]["id"]),
    )
    winner = ranked[0][1] if ranked and ranked[0][0] > tiers.segment_threshold else None
    if winner is None:
        ident = derived_id("memoryos-segment", str(_bank(ctx)), str(old["id"]))
        segment = (
            conn.execute(
                tier_segments.insert()
                .values(
                    id=ident,
                    **Scope.model_validate(ctx.model_dump()).model_dump(),
                    bank_id=_bank(ctx),
                    keywords=sorted(words),
                    embedding=vector,
                    model=model,
                    unpromoted_pages=1,
                    last_access=old["event_time"],
                )
                .returning(tier_segments)
            )
            .mappings()
            .one()
        )
        available.append(segment)
    else:
        segment = (
            conn.execute(
                tier_segments.update()
                .where(
                    visible_filter(tier_segments, ctx),
                    tier_segments.c.id == winner["id"],
                )
                .values(
                    generation=tier_segments.c.generation + 1,
                    unpromoted_pages=tier_segments.c.unpromoted_pages + 1,
                    keywords=sorted(set(winner["keywords"]) | words),
                )
                .returning(tier_segments)
            )
            .mappings()
            .one()
        )
        available = [segment if row["id"] == segment["id"] else row for row in available]
    conn.execute(
        tier_pages.update()
        .where(
            tier_pages.c.record_id == old["id"],
            tier_pages.c.bank_id == _bank(ctx),
        )
        .values(state="medium", segment_id=segment["id"])
    )
    excess = max(0, len(available) - tiers.segment_capacity)
    evicted = {
        row["id"]
        for row in sorted(
            available, key=lambda row: (_heat(row, now), row["last_access"], row["id"])
        )[:excess]
    }
    _evict(conn, ctx, evicted)
    if segment["id"] not in evicted:
        _queue_summary(conn, segment)


def project(service: MemoryService, job: Job, embedder: CachedEmbedder) -> None:
    ctx = RequestContext(**job.payload["scope"], actor={"type": "system"})
    bank = _bank(ctx)
    target = select(records).where(
        visible_filter(records, ctx),
        records.c.id == UUID(job.payload["record_id"]),
        records.c.generation == job.payload["generation"],
        records.c.is_current,
        records.c.status == "active",
        records.c.type == "turn",
    )
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        raw = conn.execute(target.with_for_update()).mappings().one_or_none()
        if (
            raw is None
            or conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None
        ):
            return
        existing = (
            conn.execute(
                select(tier_pages)
                .join(records, records.c.id == tier_pages.c.record_id)
                .where(
                    visible_filter(records, ctx),
                    tier_pages.c.record_id == raw["id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing and existing["generation"] == raw["generation"]:
            return
        if existing and existing["segment_id"]:
            _evict(conn, ctx, {existing["segment_id"]})
        statement = insert(tier_pages).values(
            record_id=raw["id"],
            generation=raw["generation"],
            bank_id=bank,
            state="short",
        )
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[tier_pages.c.record_id],
                set_={"generation": raw["generation"], "state": "short", "segment_id": None},
            )
        )
        overflow = list(
            conn.execute(
                _page_bank(ctx, bank)
                .where(tier_pages.c.state == "short")
                .order_by(records.c.event_time.desc(), records.c.id.desc())
                .offset(service.settings.tiers.short_capacity)
            ).mappings()
        )
        vectors = embedder.embed([row["content"] for row in overflow])
        for old, vector in zip(overflow, vectors, strict=True):
            _assign(conn, ctx, old, vector, service, raw["event_time"])


def summarize(service: MemoryService, job: Job) -> None:
    ctx = RequestContext(**job.payload["scope"], actor={"type": "system"})
    target = _segments(ctx).where(
        tier_segments.c.id == UUID(job.payload["segment_id"]),
        tier_segments.c.generation == job.payload["generation"],
        tier_segments.c.promotion_epoch == job.payload["epoch"],
    )
    pages_query = (
        _page_bank(ctx, _bank(ctx))
        .where(tier_pages.c.segment_id == UUID(job.payload["segment_id"]))
        .order_by(records.c.event_time.desc(), records.c.id.desc())
        .limit(64)
    )
    with service.engine.connect() as conn:
        segment = conn.execute(target).mappings().one_or_none()
        if segment is None:
            return
        rows = list(conn.execute(pages_query).mappings())
        if not rows:
            return
        current_time = max(segment["last_access"], rows[0]["event_time"])
        promoting = (
            segment["unpromoted_pages"] > 0
            and _heat(segment, current_time) > service.settings.tiers.promotion_heat
        )
        if not promoting and segment["summarized_generation"] == segment["generation"]:
            return
        available = (
            [
                item
                for item in blocks.list_visible(conn, ctx)
                if item.label.casefold()
                in {"profile", "user_profile", "user_knowledge", "agent_traits"}
            ]
            if promoting
            else []
        )
    raw_sources = {key for row in rows for key in row["source_event_ids"]}
    source_ids = raw_sources | {key for item in available for key in item.source_event_ids}
    data = {
        "promote": promoting,
        "blocks": [
            block.model_dump(mode="json", exclude={"source_event_ids"}) for block in available
        ],
        "pages": [
            {
                "id": str(row["memory_id"]),
                "time": row["event_time"].isoformat(),
                "content": row["content"],
                "source_event_ids": list(map(str, row["source_event_ids"])),
            }
            for row in reversed(rows)
        ],
    }
    result = (
        TaskRunner.for_service(service, "persona_update")
        .complete(
            "persona_update",
            ctx,
            [
                {"role": "system", "content": memoryos.PROMPT},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            schema=memoryos.SCHEMA,
            prompt_version=memoryos.VERSION,
            source_event_ids=sorted(source_ids),
            job_id=job.id,
        )
        .parsed
    )
    if result is None:
        return
    versions = {row["id"]: row["generation"] for row in rows}
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        latest = conn.execute(target.with_for_update()).mappings().one_or_none()
        current = dict(
            conn.execute(
                select(records.c.id, records.c.generation)
                .where(
                    visible_filter(records, ctx),
                    records.c.id.in_(versions),
                    records.c.is_current,
                    records.c.status == "active",
                )
                .order_by(records.c.id)
                .with_for_update()
            ).all()
        )
        if (
            latest is None
            or current != versions
            or not blocks.available_sources(conn, ctx, source_ids)
            or conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None
        ):
            return
        updates = {
            "summary": result.summary,
            "summarized_generation": latest["generation"],
            "source_event_ids": sorted(source_ids),
            "metadata": {"strategy": "memoryos", "prompt_version": memoryos.VERSION},
        }
        if promoting:
            updates.update(
                unpromoted_pages=0, visits=0, promotion_epoch=latest["promotion_epoch"] + 1
            )
        conn.execute(
            tier_segments.update()
            .where(
                visible_filter(tier_segments, ctx),
                tier_segments.c.id == latest["id"],
            )
            .values(**updates)
        )
        if promoting:
            _propose_profiles(conn, ctx, available, result.blocks, raw_sources, source_ids)


def _propose_profiles(conn, ctx, available, updates, raw_sources, source_ids) -> None:
    by_id = {block.id: block for block in available}
    for item in updates:
        base = by_id.get(item.block_id)
        if (
            base is None
            or base.revision != item.base_revision
            or base.value == item.value
            or len(item.value) > base.limit
            or not set(item.source_event_ids) <= raw_sources
        ):
            continue
        row = (
            conn.execute(
                select(memory_blocks)
                .where(
                    visible_filter(memory_blocks, ctx),
                    memory_blocks.c.id == item.block_id,
                    memory_blocks.c.head_revision == item.base_revision,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is not None and blocks.available_sources(
            conn, Scope.model_validate(dict(row)), source_ids
        ):
            blocks.propose(
                conn,
                ctx,
                row,
                item.value,
                source_ids,
                {"strategy": "memoryos", "prompt_version": memoryos.VERSION},
            )


@dataclass(frozen=True, slots=True)
class TierHit:
    row: RowMapping
    reason: str
    score: float


def recall(conn: Connection, service: BoundService, request: SearchRequest) -> list[TierHit]:
    if request.as_of_recorded is not None:
        return []
    embedder = CachedEmbedder(service.engine, make_embedding_provider(service.settings))
    vector = embedder.embed([request.query])[0]
    words = set(TOKENIZER.tokenize(request.query))
    ctx = service.ctx
    query = (
        select(records, tier_pages.c.segment_id)
        .join(
            tier_pages,
            tier_pages.c.record_id == records.c.id,
        )
        .where(*search_filters(ctx, request), tier_pages.c.generation == records.c.generation)
    )
    recent_query = (
        query.where(tier_pages.c.state == "short")
        .order_by(
            records.c.event_time.desc(),
            records.c.id.desc(),
        )
        .limit(service.settings.tiers.short_capacity)
    )
    recent = list(conn.execute(recent_query).mappings())
    samples = list(
        conn.execute(
            select(tier_segments).where(
                visible_filter(tier_segments, ctx),
                tier_segments.c.model == embedder.model,
            )
        ).mappings()
    )
    ranked = sorted(
        (
            (similarity(vector, row["embedding"], words, set(row["keywords"])), row)
            for row in samples
        ),
        key=lambda item: (-item[0], item[1]["id"]),
    )
    ids = {row["id"] for score, row in ranked[:RETRIEVE_SEGMENTS] if score > 0}
    medium = list(conn.execute(query.where(tier_pages.c.segment_id.in_(ids))).mappings())
    vectors = embedder.embed([row["content"] for row in [*recent, *medium]])
    short_hits = [
        TierHit(row, "memoryos:short", cosine(vector, item))
        for row, item in zip(recent, vectors[: len(recent)], strict=True)
    ]
    medium_hits = sorted(
        (
            TierHit(row, "memoryos:medium", cosine(vector, item))
            for row, item in zip(medium, vectors[len(recent) :], strict=True)
        ),
        key=lambda hit: (-hit.score, hit.row["id"]),
    )[: request.k]
    visited = {hit.row["segment_id"] for hit in medium_hits}
    now = datetime.now(UTC)
    if visited and request.as_of_recorded is None:
        changed = conn.execute(
            tier_segments.update()
            .where(
                visible_filter(tier_segments, ctx),
                tier_segments.c.id.in_(visited),
            )
            .values(
                visits=tier_segments.c.visits + 1,
                last_access=func.greatest(tier_segments.c.last_access, now),
            )
            .returning(tier_segments)
        ).mappings()
        for row in changed:
            if (
                row["unpromoted_pages"]
                and _heat(row, now) > service.settings.tiers.promotion_heat
                and _heat(row, now) - 1 <= service.settings.tiers.promotion_heat
            ):
                _queue_summary(conn, row)
    return [*short_hits, *medium_hits]


def _forget_segments(conn: Connection, org_id: str, condition: ColumnElement[bool]) -> None:
    target = and_(tier_segments.c.org_id == org_id, condition)
    ids = list(conn.scalars(select(tier_segments.c.id).where(target)))
    if not ids:
        return
    conn.execute(
        tier_pages.update()
        .where(tier_pages.c.segment_id.in_(ids))
        .values(segment_id=None, state="evicted")
    )
    conn.execute(
        tier_segments.delete().where(
            tier_segments.c.org_id == org_id,
            tier_segments.c.id.in_(ids),
        )
    )


def forget_records(conn: Connection, ctx: Scope, record_ids: list[UUID]) -> None:
    _forget_segments(
        conn,
        ctx.org_id,
        tier_segments.c.id.in_(
            select(tier_pages.c.segment_id).where(tier_pages.c.record_id.in_(record_ids)),
        ),
    )


def forget_source(conn: Connection, org_id: str, event_id: UUID) -> None:
    _forget_segments(conn, org_id, tier_segments.c.source_event_ids.contains([event_id]))
