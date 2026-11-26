from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import sessions, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.sessions import PutSessionState, SessionState
from memfxxk.ingest import NotFound
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _state(row: RowMapping) -> SessionState:
    return SessionState.model_validate(
        {
            **row,
            "session_id": row["id"],
            "scope": Scope.model_validate(dict(row)),
            "last_seq": row["next_seq"] - 1,
        }
    )


def read_session(
    connection: Connection, ctx: RequestContext, session_id: str, *, lock: bool = False
) -> SessionState:
    query = select(sessions).where(sessions.c.id == session_id, visible_filter(sessions, ctx))
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise NotFound
    return _state(row)


def get_session(service: BoundService, session_id: str) -> SessionState:
    with service.engine.connect() as connection:
        return read_session(connection, service.ctx, session_id)


def put_session_state(
    service: BoundService, session_id: str, request: PutSessionState
) -> SessionState:
    with transaction(service.engine) as connection:
        if request.revision == 0:
            connection.execute(
                insert(sessions)
                .values(id=session_id, **service.ctx.model_dump(exclude={"actor"}))
                .on_conflict_do_nothing()
            )
        current = read_session(connection, service.ctx, session_id, lock=True)
        service.require_resource_write(current.scope.project_id)
        if current.revision != request.revision:
            raise ServiceError(
                409,
                "revision_conflict",
                "Session revision has changed",
                {"revision": current.revision},
            )
        if request.summary_upto_seq is not None and request.summary_upto_seq > current.last_seq:
            raise ServiceError(422, "invalid_summary_sequence", "Summary covers unknown events")
        changes = request.model_dump(exclude={"revision"}, exclude_unset=True)
        if "summary" in changes and changes["summary"] is None:
            changes["summary_upto_seq"] = None
        row = (
            connection.execute(
                sessions.update()
                .where(sessions.c.id == session_id, visible_filter(sessions, service.ctx))
                .values(**changes, revision=current.revision + 1, updated_at=func.now())
                .returning(sessions)
            )
            .mappings()
            .one()
        )
        return _state(row)
