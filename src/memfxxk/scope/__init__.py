from collections.abc import Mapping

from sqlalchemy import ColumnElement, Row, and_, or_
from sqlalchemy.sql import FromClause

from memfxxk.domain import Scope

_OPTIONAL_DIMENSIONS = ("user_id", "agent_id", "project_id")


class ScopeError(ValueError):
    """The requested operation is outside the caller's scope."""


def visible_filter(table: FromClause, ctx: Scope) -> ColumnElement[bool]:
    conditions = [table.c.org_id == ctx.org_id]
    for dimension in _OPTIONAL_DIMENSIONS:
        column = table.c[dimension]
        value = getattr(ctx, dimension)
        conditions.append(
            column.is_(None) if value is None else or_(column.is_(None), column == value)
        )
    return and_(*conditions)


def resolve_write_scope(ctx: Scope, drop: set[str], *, requested: Scope | None = None) -> Scope:
    if not isinstance(drop, (set, frozenset)) or not drop.issubset(_OPTIONAL_DIMENSIONS):
        raise ScopeError("Only user_id, agent_id and project_id can be dropped")

    resolved = Scope(
        org_id=ctx.org_id,
        user_id=None if "user_id" in drop else ctx.user_id,
        agent_id=None if "agent_id" in drop else ctx.agent_id,
        project_id=None if "project_id" in drop else ctx.project_id,
    )
    if requested is not None and requested != resolved:
        raise ScopeError("The requested scope must match the context after dropping dimensions")
    return resolved


def mark_applies(mark_row: Mapping[str, object] | Row, ctx: Scope) -> bool:
    mark = mark_row._mapping if isinstance(mark_row, Row) else mark_row
    return mark["org_id"] == ctx.org_id and all(
        mark[dimension] is None or mark[dimension] == getattr(ctx, dimension)
        for dimension in _OPTIONAL_DIMENSIONS
    )
