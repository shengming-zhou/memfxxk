from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, Select, func, select
from sqlalchemy.dialects.postgresql import insert

from memfxxk.db import events, fact_proposals, jobs, memory_blocks, records, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.ids import derived_id
from memfxxk.jobs.queue import Job, enqueue, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2 import blocks
from memfxxk.m2.facts import _memory_id
from memfxxk.manage.common import lock_user_operations
from memfxxk.prompts import sleeptime
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService


def queue_compaction(
    conn: Connection, service: BoundService, memory_id: UUID, version: int
) -> None:
    if (
        service.settings.strategies.blocks != "letta"
        or not service.settings.sleeptime.after_compaction
    ):
        return
    row = conn.execute(
        select(
            records.c.id, records.c.generation, *(records.c[key] for key in Scope.model_fields)
        ).where(
            visible_filter(records, service.ctx),
            records.c.memory_id == memory_id,
            records.c.version == version,
        )
    ).one()
    enqueue(
        conn,
        "sleeptime_consolidate",
        {
            "record_id": str(row.id),
            "generation": row.generation,
            "trigger": "compaction",
            "scope": Scope.model_validate(dict(row._mapping)).model_dump(),
        },
        dedupe_key=f"sleeptime:compaction:{row.id}:{row.generation}",
    )


def _record(job: Job, ctx: Scope) -> Select:
    return select(records).where(
        visible_filter(records, ctx),
        records.c.id == UUID(job.payload["record_id"]),
        records.c.generation == job.payload["generation"],
        records.c.is_current,
        records.c.status == "active",
    )


def _closed(ctx: Scope, session_id: str, through: int) -> Select:
    tail = records.c.source_event_ids[func.cardinality(records.c.source_event_ids)]
    completed = (
        select(events.c.id)
        .where(
            events.c.id == tail,
            events.c.kind == "message",
            events.c.role == "assistant",
            visible_filter(events, ctx),
            ~events.c.redacted,
        )
        .exists()
    )
    return select(records).where(
        visible_filter(records, ctx),
        records.c.session_id == session_id,
        records.c.type == "turn",
        records.c.is_current,
        records.c.status == "active",
        records.c.source_pos["seq_to"].as_integer() <= through,
        completed,
    )


def consolidate(service: MemoryService, job: Job) -> None:
    ctx = RequestContext(**job.payload["scope"], actor={"type": "system"})
    config = service.settings.sleeptime
    with service.engine.connect() as conn:
        trigger = conn.execute(_record(job, ctx)).mappings().one_or_none()
        if trigger is None or not trigger["session_id"] or not trigger["source_pos"]:
            return
        through = trigger["source_pos"]["seq_to"]
        closed = _closed(ctx, trigger["session_id"], through)
        if job.payload.get("trigger") == "compaction":
            if not config.after_compaction:
                return
        else:
            count = conn.scalar(closed.with_only_columns(func.count()).select_from(records)) or 0
            if not config.every_turns or not count or count % config.every_turns:
                return
        history = list(
            conn.execute(
                closed.order_by(records.c.source_pos["seq_to"].as_integer().desc()).limit(
                    config.recent_turns
                )
            ).mappings()
        )
        past_summary = conn.execute(
            select(
                records.c.content, records.c.source_event_ids, records.c.id, records.c.generation
            ).where(
                visible_filter(records, ctx),
                records.c.session_id == trigger["session_id"],
                records.c.type == "summary",
                records.c.status == "active",
                records.c.is_current,
                records.c.source_pos["seq_to"].as_integer() <= through,
            )
        ).one_or_none()
        ids = {key for row in history for key in row["source_event_ids"]}
        originals = list(
            conn.execute(
                select(events)
                .where(
                    visible_filter(events, ctx),
                    events.c.id.in_(ids),
                    ~events.c.redacted,
                )
                .order_by(events.c.session_seq)
            ).mappings()
        )
        if not originals or set(item["id"] for item in originals) != ids:
            return
        available = blocks.list_visible(conn, ctx)
    source_pool = ids | set(past_summary.source_event_ids if past_summary else [])
    block_sources = set(key for item in available for key in item.source_event_ids)
    originals_data = [
        {
            "id": str(row["id"]),
            "text": row["content"],
            "role": row["role"],
            "event_time": row["event_time"].isoformat(),
        }
        for row in originals
        if row["kind"] == "message"
    ]
    data = {
        "originals": originals_data,
        "prior_summary": past_summary.content if past_summary else "",
        "blocks": [
            item.model_dump(mode="json", exclude={"source_event_ids"}) for item in available
        ],
    }
    seen_messages = {UUID(item["id"]) for item in originals_data}
    dependencies = source_pool | block_sources
    response = (
        TaskRunner.for_service(service, "sleeptime")
        .complete(
            "sleeptime",
            ctx,
            [
                {"role": "system", "content": sleeptime.PROMPT},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            schema=sleeptime.SCHEMA,
            prompt_version=sleeptime.VERSION,
            source_event_ids=sorted(dependencies),
            job_id=job.id,
        )
        .parsed
    )
    if response is None:
        return
    views = {item.id: item for item in available}
    generations = {row["id"]: row["generation"] for row in history}
    if past_summary:
        generations[past_summary.id] = past_summary.generation
    scope = Scope.model_validate(ctx.model_dump())
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        if conn.execute(_record(job, ctx).with_for_update()).first() is None:
            return
        if conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None:
            return
        current = dict(
            conn.execute(
                select(records.c.id, records.c.generation)
                .where(
                    visible_filter(records, ctx),
                    records.c.id.in_(generations),
                    records.c.is_current,
                    records.c.status == "active",
                )
                .order_by(records.c.id)
                .with_for_update()
            ).all()
        )
        if current != generations or not blocks.available_sources(conn, ctx, dependencies):
            return
        current_blocks = dict(
            conn.execute(
                select(memory_blocks.c.id, memory_blocks.c.head_revision)
                .where(
                    visible_filter(memory_blocks, ctx),
                    memory_blocks.c.id.in_(views),
                )
                .order_by(memory_blocks.c.id)
                .with_for_update()
            ).all()
        )
        if current_blocks != {item.id: item.revision for item in available}:
            return
        revised = set()
        for suggestion in response.blocks:
            baseline = views.get(suggestion.block_id)
            if (
                baseline is None
                or suggestion.block_id in revised
                or baseline.revision != suggestion.base_revision
                or suggestion.value == baseline.value
                or len(suggestion.value) > baseline.limit
                or not set(suggestion.source_event_ids) <= seen_messages
            ):
                continue
            row = (
                conn.execute(
                    select(memory_blocks)
                    .where(
                        visible_filter(memory_blocks, ctx),
                        memory_blocks.c.id == suggestion.block_id,
                        memory_blocks.c.head_revision == suggestion.base_revision,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                continue
            block_scope = Scope.model_validate(dict(row))
            if not blocks.available_sources(conn, block_scope, dependencies):
                continue
            metadata = {"strategy": "letta", "prompt_version": sleeptime.VERSION}
            if blocks.may_auto_edit(ctx, row):
                blocks.write_revision(conn, ctx, row, suggestion.value, dependencies, metadata)
            else:
                blocks.propose(conn, ctx, row, suggestion.value, dependencies, metadata)
            revised.add(suggestion.block_id)
        for fact in response.facts:
            if not set(fact.source_event_ids) <= seen_messages:
                continue
            memory_id = _memory_id(scope, " ".join(fact.key.casefold().split()))
            proposal_id = derived_id("sleeptime-fact", str(memory_id), fact.content, str(job.id))
            conn.execute(
                insert(fact_proposals)
                .values(
                    id=proposal_id,
                    **scope.model_dump(),
                    memory_id=memory_id,
                    source_event_ids=sorted(dependencies),
                    fact=fact.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
