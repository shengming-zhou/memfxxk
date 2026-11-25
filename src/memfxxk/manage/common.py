from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import events, records
from memfxxk.domain import RequestContext, Scope
from memfxxk.ids import new_id
from memfxxk.ingest import NotFound
from memfxxk.jobs import enqueue
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer, build_tsvector_literal


class ManagementError(ValueError):
    def __init__(
        self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def lock_user_operations(conn: Connection, ctx: RequestContext) -> None:
    # Serialize management operations within an organization, including duplicate link checks.
    lock_key = func.hashtextextended(f"memfxxk-user-op:{ctx.org_id}", 0)
    conn.execute(select(func.pg_advisory_xact_lock(lock_key)))


def current_record(
    conn: Connection, ctx: RequestContext, memory_id: UUID, *, lock: bool = False
) -> RowMapping:
    query = select(records).where(
        visible_filter(records, ctx), records.c.memory_id == memory_id, records.c.is_current
    )
    row = conn.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise NotFound
    return row


def write_audit(
    conn: Connection,
    ctx: RequestContext,
    op: str,
    target: UUID,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    scope: Scope | None = None,
    idempotency_key: str | None = None,
) -> UUID | None:
    selected_scope = scope if scope is not None else Scope.model_validate(ctx.model_dump())
    statement = insert(events).values(
        id=new_id(),
        **selected_scope.model_dump(),
        kind="user_op",
        actor=ctx.actor.model_dump(),
        event_time=datetime.now(UTC),
        payload={"op": op, "target": str(target), "args": args, "result": result},
        idempotency_key=idempotency_key,
    )
    if idempotency_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[events.c.org_id, events.c.idempotency_key]
        )
    return conn.scalar(statement.returning(events.c.id))


def lexemes(content: str) -> str:
    return build_tsvector_literal(MixedTokenizer().tokenize(content))


def embed_record(conn: Connection, record_id: UUID, generation: int) -> None:
    enqueue(
        conn,
        "embed_record",
        {"record_id": str(record_id), "generation": generation},
        dedupe_key=f"embed:{record_id}",
    )
