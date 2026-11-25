from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import events, memory_marks, memory_relations, records, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.manage import (
    LinkRequest,
    LinkResult,
    MemoryVersion,
    PatchRequest,
    RememberRequest,
)
from memfxxk.ids import new_id
from memfxxk.ingest import NotFound
from memfxxk.scope import resolve_write_scope, visible_filter

from .common import (
    ManagementError,
    current_record,
    embed_record,
    lexemes,
    lock_user_operations,
    write_audit,
)

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _set_mark(
    conn: Connection,
    ctx: RequestContext,
    scope: Scope,
    memory_id: UUID,
    mark: str,
    enabled: bool | None,
    source_event_id: UUID,
) -> None:
    if enabled is True:
        conn.execute(
            insert(memory_marks)
            .values(
                id=new_id(),
                memory_id=memory_id,
                mark=mark,
                **scope.model_dump(),
                created_by=ctx.actor.model_dump(),
                source_event_id=source_event_id,
            )
            .on_conflict_do_nothing()
        )
    elif enabled is False:
        conn.execute(
            memory_marks.delete().where(
                visible_filter(memory_marks, ctx),
                memory_marks.c.memory_id == memory_id,
                memory_marks.c.mark == mark,
                *(memory_marks.c[key] == value for key, value in scope.model_dump().items()),
            )
        )


def _duplicate_remember(conn: Connection, ctx: RequestContext, key: str) -> MemoryVersion:
    row = conn.execute(
        select(events.c.kind, events.c.payload, events.c.redacted).where(
            visible_filter(events, ctx), events.c.idempotency_key == key
        )
    ).one_or_none()
    if row is None or row.redacted:
        raise NotFound
    if row.kind != "user_op" or row.payload.get("op") != "remember":
        raise ManagementError(409, "idempotency_conflict", "This key belongs to another operation")
    try:
        result = MemoryVersion.model_validate(row.payload.get("result"))
    except ValidationError as error:
        raise ManagementError(
            409, "idempotency_conflict", "This key belongs to another operation"
        ) from error
    current_record(conn, ctx, result.memory_id)
    return result


def _source_event(
    conn: Connection, ctx: RequestContext, scope: Scope, event_id: UUID
) -> RowMapping:
    row = (
        conn.execute(
            select(events)
            .where(
                events.c.id == event_id, visible_filter(events, ctx), visible_filter(events, scope)
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["redacted"]:
        raise NotFound
    if row["content"] is None:
        raise ManagementError(422, "missing_content", "The source event has no text")
    return row


def remember(service: BoundService, request: RememberRequest) -> MemoryVersion:
    ctx = service.ctx
    scope = resolve_write_scope(ctx, request.scope_drop)
    memory_id = new_id()
    result = MemoryVersion(memory_id=memory_id, version=1)
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        source_event_id = write_audit(
            conn,
            ctx,
            "remember",
            memory_id,
            request.model_dump(mode="json"),
            result.model_dump(mode="json"),
            scope=scope if not request.links else None,
            idempotency_key=request.idempotency_key,
        )
        if source_event_id is None:
            assert request.idempotency_key is not None
            return _duplicate_remember(conn, ctx, request.idempotency_key)

        content = request.content
        event_time = datetime.now(UTC)
        source_ids = [source_event_id]
        source_pos = None
        session_id = None
        if request.from_event_id is not None:
            source = _source_event(conn, ctx, scope, request.from_event_id)
            content = source["content"]
            if request.span is not None:
                if request.span.end > len(content):
                    raise ManagementError(422, "invalid_span", "Span exceeds the source text")
                content = content[request.span.start : request.span.end]
                source_pos = request.span.model_dump()
            event_time = source["event_time"]
            source_ids = [request.from_event_id]
            session_id = source["session_id"]
        assert content is not None
        record_id = new_id()
        conn.execute(
            records.insert().values(
                id=record_id,
                memory_id=memory_id,
                version=1,
                **scope.model_dump(),
                type=request.type,
                content=content,
                title=request.title,
                source_event_ids=source_ids,
                source_pos=source_pos,
                session_id=session_id,
                origin="user_explicit",
                confirmed=True,
                event_time=event_time,
                lexemes=lexemes(content),
                created_by=ctx.actor.model_dump(),
            )
        )
        for mark, enabled in (("important", request.important), ("pinned", request.pinned)):
            if enabled:
                _set_mark(conn, ctx, scope, memory_id, mark, enabled, source_event_id)
        for link_item in request.links:
            related = LinkRequest(
                from_memory_id=memory_id,
                to_memory_id=link_item.memory_id,
                purpose=link_item.purpose,
            )
            current_record(conn, ctx, related.to_memory_id, lock=True)
            _add_relation(conn, ctx, related, source_event_id)
        embed_record(conn, record_id, 0)
    return result


def patch_memory(service: BoundService, memory_id: UUID, request: PatchRequest) -> MemoryVersion:
    ctx = service.ctx
    mark_scope = resolve_write_scope(ctx, request.mark_scope_drop)
    changes = request.model_fields_set & {"content", "title"}
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        current = current_record(conn, ctx, memory_id, lock=True)
        version = current["version"]
        if changes:
            if current["type"] not in ("note", "fact", "lesson"):
                raise ManagementError(422, "immutable_record", "This kind of memory is immutable")
            if request.base_version is None:
                raise ManagementError(422, "base_version_required", "Provide a base_version")
            if request.base_version != version:
                raise ManagementError(
                    409, "version_conflict", "The memory has changed", {"current_version": version}
                )
            if "content" in changes and request.content is None:
                raise ManagementError(422, "invalid_content", "Content must be text")
            record_id = new_id()
            new_values = {**current, **{key: getattr(request, key) for key in changes}}
            new_values.pop("recorded_at")
            version += 1
            new_values.update(
                id=record_id,
                version=version,
                is_current=True,
                confirmed=True,
                created_by=ctx.actor.model_dump(),
                generation=0,
                lexemes=lexemes(new_values["content"]),
            )
            conn.execute(
                records.update()
                .where(records.c.id == current["id"], visible_filter(records, ctx))
                .values(is_current=False)
            )
            conn.execute(records.insert().values(**new_values))
            embed_record(conn, record_id, 0)
        result = MemoryVersion(memory_id=memory_id, version=version)
        audit_id = write_audit(
            conn,
            ctx,
            "patch_memory",
            memory_id,
            request.model_dump(mode="json", exclude_unset=True),
            result.model_dump(mode="json"),
        )
        assert audit_id is not None
        _set_mark(conn, ctx, mark_scope, memory_id, "important", request.important, audit_id)
        _set_mark(conn, ctx, mark_scope, memory_id, "pinned", request.pinned, audit_id)
        from memfxxk.m2.preferences import write_preferences

        write_preferences(conn, mark_scope, memory_id, request)
    return result


def _relation_id(conn: Connection, ctx: RequestContext, request: LinkRequest) -> UUID | None:
    return conn.scalar(
        select(memory_relations.c.id).where(
            memory_relations.c.org_id == ctx.org_id,
            memory_relations.c.from_memory_id == request.from_memory_id,
            memory_relations.c.to_memory_id == request.to_memory_id,
            memory_relations.c.purpose == request.purpose,
        )
    )


def _add_relation(
    conn: Connection, ctx: RequestContext, request: LinkRequest, source_event_id: UUID
) -> UUID:
    relation_id = _relation_id(conn, ctx, request)
    if relation_id is None:
        relation_id = new_id()
        conn.execute(
            memory_relations.insert().values(
                id=relation_id,
                org_id=ctx.org_id,
                **request.model_dump(),
                created_by=ctx.actor.model_dump(),
                source_event_id=source_event_id,
            )
        )
    return relation_id


def link(service: BoundService, request: LinkRequest) -> LinkResult:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        for memory_id in sorted({request.from_memory_id, request.to_memory_id}):
            current_record(conn, ctx, memory_id, lock=True)
        if request.from_memory_id == request.to_memory_id:
            raise ManagementError(422, "self_relation", "A memory cannot link to itself")
        duplicate = _relation_id(conn, ctx, request)
        result = LinkResult(relation_id=duplicate if duplicate is not None else new_id())
        audit_id = write_audit(
            conn,
            ctx,
            "link",
            request.from_memory_id,
            request.model_dump(mode="json"),
            result.model_dump(mode="json"),
        )
        if duplicate is None:
            conn.execute(
                memory_relations.insert().values(
                    id=result.relation_id,
                    org_id=ctx.org_id,
                    **request.model_dump(),
                    created_by=ctx.actor.model_dump(),
                    source_event_id=audit_id,
                )
            )
    return result


def unlink(service: BoundService, relation_id: UUID) -> None:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        row = conn.execute(
            select(memory_relations.c.from_memory_id, memory_relations.c.to_memory_id).where(
                memory_relations.c.org_id == ctx.org_id, memory_relations.c.id == relation_id
            )
        ).one_or_none()
        if row is None:
            raise NotFound
        visible = conn.scalars(
            select(records.c.memory_id).where(
                visible_filter(records, ctx),
                records.c.is_current,
                records.c.memory_id.in_((row.from_memory_id, row.to_memory_id)),
            )
        ).all()
        if not visible:
            raise NotFound
        conn.execute(
            memory_relations.delete().where(
                memory_relations.c.org_id == ctx.org_id, memory_relations.c.id == relation_id
            )
        )
        write_audit(
            conn, ctx, "unlink", visible[0], {"relation_id": str(relation_id)}, {"deleted": True}
        )
