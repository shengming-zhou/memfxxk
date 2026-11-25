from collections.abc import Callable

import pytest

from memfxxk.domain import RequestContext, Scope
from memfxxk.scope import ScopeError, mark_applies, resolve_write_scope


def test_write_scope_can_drop_dimensions(make_ctx: Callable[..., RequestContext]) -> None:
    ctx = make_ctx(user="u1", agent="a1", project="p1")

    assert resolve_write_scope(ctx, set()) == Scope(
        org_id=ctx.org_id, user_id="u1", agent_id="a1", project_id="p1"
    )
    assert resolve_write_scope(ctx, {"agent_id", "project_id"}) == Scope(
        org_id=ctx.org_id, user_id="u1"
    )


@pytest.mark.parametrize("dimension", ["org_id", "user_id", "agent_id", "project_id"])
def test_write_scope_rejects_changed_values(
    make_ctx: Callable[..., RequestContext], dimension: str
) -> None:
    ctx = make_ctx(user="u1", agent="a1", project="p1")
    scope = Scope.model_validate(ctx.model_dump())
    requested = scope.model_copy(update={dimension: "different"})

    with pytest.raises(ScopeError):
        resolve_write_scope(ctx, set(), requested=requested)


@pytest.mark.parametrize("dimension", ["org_id", "session_id"])
def test_write_scope_rejects_dropping_invalid_dimensions(
    make_ctx: Callable[..., RequestContext], dimension: str
) -> None:
    with pytest.raises(ScopeError):
        resolve_write_scope(make_ctx(), {dimension})


@pytest.mark.parametrize(
    ("mark_org", "mark_user", "mark_project", "ctx_user", "ctx_project", "expected"),
    [
        ("test-org", None, "p1", "u2", "p1", True),
        ("test-org", None, "p1", "u2", None, False),
        ("test-org", "u1", None, "u1", "p2", True),
        ("test-org", "u1", None, "u2", "p1", False),
        ("outside-org", None, None, "u1", "p1", False),
    ],
)
def test_marks_obey_scope(
    make_ctx: Callable[..., RequestContext],
    mark_org: str,
    mark_user: str | None,
    mark_project: str | None,
    ctx_user: str | None,
    ctx_project: str | None,
    expected: bool,
) -> None:
    mark = {
        "org_id": mark_org,
        "user_id": mark_user,
        "agent_id": None,
        "project_id": mark_project,
    }
    assert mark_applies(mark, make_ctx(user=ctx_user, project=ctx_project)) is expected
