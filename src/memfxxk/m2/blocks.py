from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import block_proposals, block_revisions, events, memory_blocks, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.blocks import (
    BlockEditResult,
    BlockProposal,
    CreateBlock,
    EditBlock,
    MemoryBlock,
)
from memfxxk.ids import derived_id, new_id
from memfxxk.ingest import NotFound
from memfxxk.manage.common import ManagementError, lock_user_operations, write_audit
from memfxxk.scope import resolve_write_scope, visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _block(conn: Connection, ctx: Scope, block_id: UUID, *, lock: bool = False) -> RowMapping:
    query = select(memory_blocks).where(
        visible_filter(memory_blocks, ctx),
        memory_blocks.c.id == block_id,
    )
    row = conn.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise NotFound
    return row


def view(conn: Connection, ctx: Scope, row: RowMapping) -> MemoryBlock:
    version = conn.execute(
        select(block_revisions.c.value, block_revisions.c.source_event_ids)
        .join(memory_blocks, memory_blocks.c.id == block_revisions.c.block_id)
        .where(
            visible_filter(memory_blocks, ctx),
            block_revisions.c.block_id == row["id"],
            block_revisions.c.revision == row["head_revision"],
        )
    ).one_or_none()
    value = version.value if version else ""
    return MemoryBlock(
        id=row["id"],
        label=row["label"],
        description=row["description"],
        owner=row["owner"],
        owner_id=row["owner_id"],
        read_only=row["read_only"],
        limit=row["limit_chars"],
        char_count=len(value),
        value=value,
        revision=row["head_revision"],
        source_event_ids=version.source_event_ids if version else [],
    )


def list_visible(conn: Connection, ctx: Scope) -> list[MemoryBlock]:
    rows = conn.execute(
        select(memory_blocks)
        .where(visible_filter(memory_blocks, ctx))
        .order_by(memory_blocks.c.label, memory_blocks.c.id)
    ).mappings()
    return [view(conn, ctx, row) for row in rows]


def get(service: BoundService, block_id: UUID) -> MemoryBlock:
    with service.engine.connect() as conn:
        return view(conn, service.ctx, _block(conn, service.ctx, block_id))


def list_blocks(service: BoundService) -> list[MemoryBlock]:
    with service.engine.connect() as conn:
        return list_visible(conn, service.ctx)


def _limit(value: str, limit: int, current: int) -> None:
    if len(value) > limit:
        raise ManagementError(
            422,
            "block_limit_exceeded",
            "The replacement is larger than the character limit",
            {"char_count": current, "requested_char_count": len(value), "limit": limit},
        )


def _check_revision(current: MemoryBlock, expected: int) -> None:
    if current.revision != expected:
        raise ManagementError(
            409,
            "block_revision_conflict",
            "The block has changed",
            {"revision": current.revision, "char_count": current.char_count},
        )


def available_sources(conn: Connection, ctx: Scope, ids: set[UUID]) -> bool:
    return (
        bool(ids)
        and set(
            conn.scalars(
                select(events.c.id).where(
                    visible_filter(events, ctx),
                    events.c.id.in_(ids),
                    ~events.c.redacted,
                )
            )
        )
        == ids
    )


def write_revision(
    conn: Connection,
    ctx: RequestContext,
    row: RowMapping,
    value: str,
    source_ids: set[UUID],
) -> int:
    revision = row["revision_seq"] + 1
    conn.execute(
        block_revisions.insert().values(
            block_id=row["id"],
            revision=revision,
            value=value,
            source_event_ids=sorted(source_ids),
            actor=ctx.actor.model_dump(),
        )
    )
    conn.execute(
        memory_blocks.update()
        .where(memory_blocks.c.id == row["id"], visible_filter(memory_blocks, ctx))
        .values(head_revision=revision, revision_seq=revision)
    )
    return revision


def _named_scope(ctx: RequestContext, owner: str) -> str | None:
    return ctx.user_id if owner == "user" else ctx.agent_id


def create(service: BoundService, request: CreateBlock) -> MemoryBlock:
    service.require_write(request.scope_drop)
    ctx = service.ctx
    owner_id = _named_scope(ctx, request.owner)
    if owner_id is None:
        raise ManagementError(422, "block_missing_owner", "The requested owner has no scoped ID")
    if request.owner == "user" and (
        ctx.actor.type != "user" or service.principal.role == "project"
    ):
        raise ManagementError(
            403, "block_requires_user", "Use a user actor to create a user-owned block"
        )
    scope = resolve_write_scope(ctx, request.scope_drop)
    if not request.label.strip() or not request.description.strip():
        raise ManagementError(422, "invalid_block_label", "Provide a block label and description")
    _limit(request.value, request.limit, 0)
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        identifier = new_id()
        saved = conn.scalar(
            insert(memory_blocks)
            .values(
                id=identifier,
                **scope.model_dump(),
                label=request.label.strip(),
                description=request.description.strip(),
                owner=request.owner,
                owner_id=owner_id,
                read_only=(request.owner == "user")
                if request.read_only is None
                else request.read_only,
                limit_chars=request.limit,
                head_revision=0,
                revision_seq=0,
            )
            .on_conflict_do_nothing()
            .returning(memory_blocks.c.id)
        )
        if saved is None:
            raise ManagementError(
                409, "duplicate_block_label", "The block label already exists in this scope"
            )
        audit = write_audit(
            conn,
            ctx,
            "create_block",
            identifier,
            request.model_dump(mode="json"),
            {"revision": 1},
            scope=scope,
        )
        assert audit is not None
        write_revision(conn, ctx, _block(conn, ctx, identifier), request.value, {audit})
        return view(conn, ctx, _block(conn, ctx, identifier))


def _is_user_owner(service: BoundService, row: RowMapping) -> bool:
    ctx = service.ctx
    return (
        ctx.actor.type == "user"
        and service.principal.role != "project"
        and (
            service.principal.role == "admin"
            or (row["owner"] == "user" and row["owner_id"] == ctx.user_id)
            or (
                row["owner"] == "agent"
                and ctx.user_id is not None
                and row["user_id"] == ctx.user_id
            )
        )
    )


def may_auto_edit(ctx: RequestContext, row: RowMapping) -> bool:
    return row["owner"] == "agent" and not row["read_only"] and ctx.agent_id == row["owner_id"]


def _replacement(current: MemoryBlock, request: EditBlock) -> str:
    if request.operation == "rewrite":
        return request.text
    if request.operation == "append":
        return "\n".join(piece for piece in (current.value, request.text) if piece)
    assert request.old_text is not None
    if current.value.count(request.old_text) != 1:
        raise ManagementError(
            409, "block_replace_target", "The source text must match exactly once"
        )
    return current.value.replace(request.old_text, request.text, 1)


def propose(
    conn: Connection,
    ctx: RequestContext,
    row: RowMapping,
    value: str,
    sources: set[UUID],
) -> BlockProposal:
    identifier = derived_id(
        "block-proposal",
        str(row["id"]),
        str(row["head_revision"]),
        value,
        *map(str, sorted(sources)),
    )
    conn.execute(
        insert(block_proposals)
        .values(
            id=identifier,
            **Scope.model_validate(dict(row)).model_dump(),
            block_id=row["id"],
            base_revision=row["head_revision"],
            value=value,
            source_event_ids=sorted(sources),
        )
        .on_conflict_do_nothing()
    )
    return BlockProposal.model_validate(
        conn.execute(
            select(block_proposals).where(
                visible_filter(block_proposals, ctx),
                block_proposals.c.id == identifier,
            )
        )
        .mappings()
        .one()
    )


def edit(service: BoundService, block_id: UUID, request: EditBlock) -> BlockEditResult:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        row = _block(conn, ctx, block_id, lock=True)
        service.require_resource_write(row["project_id"])
        current = view(conn, ctx, row)
        _check_revision(current, request.base_revision)
        proposed = _replacement(current, request)
        _limit(proposed, current.limit, current.char_count)
        if proposed == current.value:
            return BlockEditResult(block=current)
        sources = set(request.source_event_ids)
        row_scope = Scope.model_validate(dict(row))
        if sources and not available_sources(conn, row_scope, sources):
            raise NotFound
        if request.operation != "rewrite" or not _is_user_owner(service, row):
            sources.update(current.source_event_ids)
        audit_args = request.model_dump(
            mode="json", exclude={"text", "old_text"} if request.source_event_ids else set()
        )
        audit = write_audit(
            conn,
            ctx,
            "edit_block",
            block_id,
            audit_args,
            {"base_revision": current.revision},
            scope=row_scope,
        )
        assert audit is not None
        sources.add(audit)
        if not (_is_user_owner(service, row) or may_auto_edit(ctx, row)):
            return BlockEditResult(
                block=current, proposal=propose(conn, ctx, row, proposed, sources)
            )
        write_revision(conn, ctx, row, proposed, sources)
        return BlockEditResult(block=view(conn, ctx, _block(conn, ctx, block_id)))


def list_proposals(service: BoundService) -> list[BlockProposal]:
    with service.engine.connect() as conn:
        rows = conn.execute(
            select(block_proposals)
            .where(
                visible_filter(block_proposals, service.ctx), block_proposals.c.status == "pending"
            )
            .order_by(block_proposals.c.created_at.desc(), block_proposals.c.id)
        ).mappings()
        return [BlockProposal.model_validate(row) for row in rows]


def review(service: BoundService, proposal_id: UUID, accept: bool) -> BlockProposal:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        target = (visible_filter(block_proposals, ctx), block_proposals.c.id == proposal_id)
        pending = (
            conn.execute(select(block_proposals).where(*target).with_for_update())
            .mappings()
            .one_or_none()
        )
        if pending is None:
            raise NotFound
        row = _block(conn, ctx, pending["block_id"], lock=True)
        service.require_resource_write(row["project_id"])
        if not _is_user_owner(service, row):
            raise ManagementError(
                403, "block_requires_user", "The block owner must review this proposal"
            )
        status = "accepted" if accept else "rejected"
        if pending["status"] == status:
            return BlockProposal.model_validate(pending)
        if pending["status"] != "pending":
            raise ManagementError(
                409, "block_proposal_reviewed", "This proposal was already reviewed"
            )
        revision = None
        if accept:
            current = view(conn, ctx, row)
            _check_revision(current, pending["base_revision"])
            sources = set(pending["source_event_ids"])
            if not available_sources(conn, Scope.model_validate(dict(row)), sources):
                raise NotFound
            revision = write_revision(conn, ctx, row, pending["value"], sources)
        saved = (
            conn.execute(
                block_proposals.update()
                .where(*target)
                .values(status=status, adopted_revision=revision)
                .returning(block_proposals)
            )
            .mappings()
            .one()
        )
        write_audit(
            conn,
            ctx,
            "review_block",
            row["id"],
            {"proposal_id": str(proposal_id), "accept": accept},
            {"revision": revision},
        )
        return BlockProposal.model_validate(saved)


def forget_source(conn: Connection, org_id: str, event_id: UUID) -> None:
    tenant = memory_blocks.c.org_id == org_id
    block_ids = select(memory_blocks.c.id).where(tenant)
    removed = conn.execute(
        block_revisions.delete()
        .where(
            block_revisions.c.block_id.in_(block_ids),
            block_revisions.c.source_event_ids.contains([event_id]),
        )
        .returning(block_revisions.c.block_id, block_revisions.c.revision)
    ).all()
    conn.execute(
        block_proposals.delete().where(
            block_proposals.c.org_id == org_id,
            block_proposals.c.source_event_ids.contains([event_id]),
        )
    )
    for key, revision in removed:
        remaining = conn.scalar(
            select(block_revisions.c.revision)
            .where(block_revisions.c.block_id == key)
            .order_by(block_revisions.c.revision.desc())
            .limit(1)
        )
        conn.execute(
            memory_blocks.update()
            .where(
                tenant,
                memory_blocks.c.id == key,
                memory_blocks.c.head_revision == revision,
            )
            .values(head_revision=remaining or 0)
        )
