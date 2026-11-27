from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, and_, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from memfxxk.db import (
    compactions,
    events,
    fact_proposals,
    memory_marks,
    memory_preferences,
    memory_relations,
    record_embeddings,
    records,
    sessions,
    transaction,
)
from memfxxk.domain import RequestContext
from memfxxk.domain.manage import ForgetMode, ForgetResult
from memfxxk.ingest import NotFound
from memfxxk.llm.runner import redact_source_calls
from memfxxk.scope import visible_filter

from .common import ManagementError, current_record, embed_record, lock_user_operations, write_audit

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _authorize_delete(service: BoundService) -> None:
    if denial := service.hard_delete_denial():
        raise ManagementError(403, "forbidden", denial)


def _purge_memories(conn: Connection, ctx: RequestContext, memory_ids: set[UUID]) -> ForgetResult:
    if not memory_ids:
        return ForgetResult()

    # These IDs are derived from the already authorized record or event. Delete all versions
    # and dependent rows under the same tenant, including marks in another project.
    record_filter = and_(records.c.org_id == ctx.org_id, records.c.memory_id.in_(memory_ids))
    summary_sessions = select(records.c.session_id).where(
        record_filter,
        records.c.type == "summary",
        records.c.is_current,
    )
    conn.execute(
        sessions.update()
        .where(
            sessions.c.org_id == ctx.org_id,
            sessions.c.id.in_(summary_sessions),
        )
        .values(summary=None, summary_upto_seq=None, revision=sessions.c.revision + 1)
    )
    record_ids = conn.scalars(
        select(records.c.id).where(record_filter).order_by(records.c.id).with_for_update()
    ).all()
    embeddings = conn.scalar(
        select(func.count())
        .select_from(record_embeddings)
        .where(record_embeddings.c.record_id.in_(record_ids))
    )
    from memfxxk.qdrant import queue_removal

    queue_removal(conn, record_ids)
    conn.execute(records.delete().where(record_filter))
    for derived in (memory_preferences, fact_proposals, compactions):
        conn.execute(
            derived.delete().where(
                derived.c.org_id == ctx.org_id, derived.c.memory_id.in_(memory_ids)
            )
        )
    marks = conn.execute(
        memory_marks.delete().where(
            memory_marks.c.org_id == ctx.org_id, memory_marks.c.memory_id.in_(memory_ids)
        )
    ).rowcount
    relations = conn.execute(
        memory_relations.delete().where(
            memory_relations.c.org_id == ctx.org_id,
            or_(
                memory_relations.c.from_memory_id.in_(memory_ids),
                memory_relations.c.to_memory_id.in_(memory_ids),
            ),
        )
    ).rowcount
    payload = events.c.payload
    target_ids = [str(memory_id) for memory_id in memory_ids]
    redacted = conn.scalars(
        events.update()
        .where(
            events.c.org_id == ctx.org_id,
            events.c.kind == "user_op",
            ~events.c.redacted,
            or_(
                payload["target"].as_string().in_(target_ids),
                and_(
                    payload["op"].as_string() == "link",
                    payload["args"]["to_memory_id"].as_string().in_(target_ids),
                ),
            ),
        )
        .values(
            content=None,
            payload=func.jsonb_build_object(
                "op", payload["op"], "target", payload["target"], "redacted", True, type_=JSONB
            ),
            redacted=True,
        )
        .returning(events.c.id)
    ).all()
    redact_source_calls(conn, ctx, redacted)
    return ForgetResult(
        records=len(record_ids),
        embeddings=embeddings or 0,
        marks=marks,
        relations=relations,
        events_redacted=len(redacted),
    )


def forget(service: BoundService, memory_id: UUID, mode: ForgetMode) -> ForgetResult:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        current = current_record(conn, ctx, memory_id, lock=True)
        service.require_resource_write(current["project_id"])
        if mode == "delete":
            _authorize_delete(service)
            result = _purge_memories(conn, ctx, {memory_id})
        else:
            status = "inactive" if mode == "deactivate" else "active"
            result = ForgetResult()
            if current["status"] != status:
                generation = current["generation"] + 1
                conn.execute(
                    records.update()
                    .where(records.c.id == current["id"], visible_filter(records, ctx))
                    .values(status=status, generation=generation)
                )
                if status == "active":
                    embed_record(conn, current["id"], generation)
                result = ForgetResult(records=1)
        write_audit(conn, ctx, "forget", memory_id, {"mode": mode}, result.model_dump())
    return result


def forget_event(service: BoundService, event_id: UUID) -> ForgetResult:
    """Delete source and derived records. A later event can rebuild the rest of its turn."""
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        target = and_(events.c.id == event_id, visible_filter(events, ctx))
        row = conn.execute(
            select(events.c.session_id, events.c.project_id).where(target)
        ).one_or_none()
        if row is None:
            raise NotFound
        service.require_resource_write(row.project_id)
        _authorize_delete(service)
        if row.session_id is not None:
            # Take the same row lock as ingest before deleting an event used to rebuild a turn.
            conn.execute(
                select(sessions.c.id)
                .where(sessions.c.id == row.session_id, visible_filter(sessions, ctx))
                .with_for_update()
            ).all()
        if conn.scalar(select(events.c.id).where(target).with_for_update()) is None:
            raise NotFound
        memory_ids = set(
            conn.scalars(
                select(records.c.memory_id).where(
                    records.c.org_id == ctx.org_id, records.c.source_event_ids.contains([event_id])
                )
            )
        )
        result = _purge_memories(conn, ctx, memory_ids)
        for derived in (fact_proposals, compactions):
            conn.execute(
                derived.delete().where(
                    derived.c.org_id == ctx.org_id, derived.c.source_event_ids.contains([event_id])
                )
            )
        redact_source_calls(conn, ctx, [event_id])
        conn.execute(events.delete().where(target))
        write_audit(conn, ctx, "forget_event", event_id, {}, result.model_dump())
    return result
