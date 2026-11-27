from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection

from memfxxk.db import records
from memfxxk.domain import RequestContext, Scope
from memfxxk.ids import new_id
from memfxxk.manage.common import embed_record, lexemes
from memfxxk.scope import visible_filter


def derived_record(
    conn: Connection,
    ctx: RequestContext,
    scope: Scope,
    *,
    memory_id: UUID,
    version: int,
    kind: str,
    content: str,
    sources: list[UUID],
    event_time: datetime,
    session_id: str | None = None,
    source_pos: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    confirmed: bool = False,
    status: Literal["active", "inactive"] = "active",
) -> None:
    conn.execute(
        records.update()
        .where(records.c.memory_id == memory_id, records.c.is_current, visible_filter(records, ctx))
        .values(is_current=False)
    )
    record_id = new_id()
    conn.execute(
        records.insert().values(
            id=record_id,
            memory_id=memory_id,
            version=version,
            **scope.model_dump(),
            session_id=session_id,
            source_pos=source_pos,
            source_event_ids=sources,
            type=kind,
            content=content,
            origin="auto_extract",
            confirmed=confirmed,
            event_time=event_time,
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
            lexemes=lexemes(content),
            created_by=ctx.actor.model_dump(),
            metadata=metadata or {},
        )
    )
    if status == "active":
        embed_record(conn, record_id, 0)
