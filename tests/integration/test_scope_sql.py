from collections.abc import Callable
from itertools import product

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from memfxxk.db import sessions
from memfxxk.domain import RequestContext
from memfxxk.scope import visible_filter

pytestmark = pytest.mark.integration


def test_visibility_matches_full_scope_matrix(
    engine: Engine, make_ctx: Callable[..., RequestContext]
) -> None:
    user_values = (None, "u1", "u2")
    agent_values = (None, "a1", "a2")
    project_values = (None, "p1", "p2")
    contexts = list(product(user_values, agent_values, project_values))
    rows = [
        {
            "id": f"session-{index}",
            "org_id": org,
            "user_id": user,
            "agent_id": agent,
            "project_id": project,
        }
        for index, (org, (user, agent, project)) in enumerate(
            product(("test-org", "outside-org"), contexts)
        )
    ]

    with engine.begin() as connection:
        connection.execute(sessions.insert(), rows)
        for user, agent, project in contexts:
            ctx = make_ctx(user=user, agent=agent, project=project)
            visible = set(
                connection.scalars(select(sessions.c.id).where(visible_filter(sessions, ctx)))
            )
            expected = {
                row["id"]
                for row in rows
                if row["org_id"] == ctx.org_id
                and row["user_id"] in (None, user)
                and row["agent_id"] in (None, agent)
                and row["project_id"] in (None, project)
            }
            assert visible == expected, (user, agent, project)
