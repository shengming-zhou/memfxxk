from uuid import UUID

from sqlalchemy import ColumnElement, Connection, Integer, select

from memfxxk.db import memory_preferences
from memfxxk.domain import Scope
from memfxxk.domain.manage import PatchRequest
from memfxxk.ids import new_id
from memfxxk.scope import visible_filter


def preference_order() -> tuple[ColumnElement[int], ColumnElement[UUID]]:
    specificity = sum(
        memory_preferences.c[name].is_not(None).cast(Integer)
        for name in ("user_id", "project_id", "agent_id")
    )
    return specificity.desc(), memory_preferences.c.id.desc()


def preferences_for(
    conn: Connection, ctx: Scope, memory_ids: list[UUID]
) -> dict[UUID, tuple[int | None, list[str]]]:
    selected: dict[UUID, tuple[int | None, list[str]]] = {}
    for row in conn.execute(
        select(memory_preferences)
        .where(
            memory_preferences.c.memory_id.in_(memory_ids), visible_filter(memory_preferences, ctx)
        )
        .order_by(*preference_order())
    ):
        priority = int(row.priority) if row.priority is not None else None
        previous = selected.setdefault(row.memory_id, (priority, row.tags))
        if previous[0] is None and priority is not None:
            selected[row.memory_id] = (priority, previous[1])
    return selected


def write_preferences(
    conn: Connection, scope: Scope, memory_id: UUID, request: PatchRequest
) -> None:
    changes = request.model_dump(include={"priority", "tags"}, exclude_unset=True)
    if not changes:
        return
    if "tags" in changes:
        changes["tags"] = changes["tags"] or []
    coordinates = scope.model_dump()
    existing = conn.scalar(
        select(memory_preferences.c.id).where(
            memory_preferences.c.memory_id == memory_id,
            visible_filter(memory_preferences, scope),
            *(memory_preferences.c[name] == value for name, value in coordinates.items()),
        )
    )
    if existing is None:
        conn.execute(
            memory_preferences.insert().values(
                id=new_id(), memory_id=memory_id, **coordinates, **changes
            )
        )
    else:
        conn.execute(
            memory_preferences.update().where(memory_preferences.c.id == existing).values(**changes)
        )
