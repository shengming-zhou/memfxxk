from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select

from memfxxk.db import memory_preferences
from memfxxk.domain import Principal
from memfxxk.domain.manage import PatchRequest, RememberRequest
from memfxxk.domain.priorities import ClearPriority, PriorityTarget, SetPriority
from memfxxk.domain.retrieval import SearchRequest

pytestmark = pytest.mark.integration

BOOK = "world/east/ports.md"
OUTLINE = "# Harbors\nThe gates open at dawn.\n## Lanterns\nRed lanterns mean a storm."


@pytest.fixture
def priority_api(api_client: TestClient):
    owner = {"X-Memfxxk-User": "author", "X-Memfxxk-Project": "novel"}
    path = f"/v1/documents/{BOOK}"
    created = api_client.put(path, headers=owner, json={"content": OUTLINE})
    assert created.status_code == 200

    def post(method, route, data, headers=None):
        return api_client.request(
            method,
            f"/v1/priorities{route}",
            json=data,
            headers=owner if headers is None else headers,
        )

    return post, owner


def set_value(request, target, value, *, scope_drop=()):
    result = request(
        "PUT", "", {"target": target, "priority": value, "scope_drop": list(scope_drop)}
    )
    assert result.status_code == 200, result.text
    return result.json()


def lookup(request, target, headers=None):
    result = request("POST", "/resolve", target, headers)
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize("target", ["world", "world/east"])
def test_directory_inherits_project_and_uses_closest_component(priority_api, target):
    request, _ = priority_api
    set_value(request, {"kind": "project"}, -10)
    set_value(request, {"kind": "directory", "path": target}, 27)
    inside = lookup(request, {"kind": "document", "path": BOOK})
    assert inside["priority"] == 27 and inside["source"]["path"] == target
    sibling = lookup(request, {"kind": "directory", "path": "worldwide"})
    assert sibling["priority"] == -10 and sibling["source"]["kind"] == "project"


def test_priority_overrides_are_exact_and_clear_restores_inheritance(priority_api):
    request, _ = priority_api
    doc = {"kind": "document", "path": BOOK}
    chapter = {"kind": "section", "path": BOOK, "heading": "Harbors"}
    subsection = {**chapter, "heading": "Harbors / Lanterns"}
    value = 10**70
    set_value(request, {"kind": "directory", "path": "world/"}, 8)
    assert set_value(request, doc, value)["priority"] == value
    assert lookup(request, chapter)["priority"] == value
    set_value(request, chapter, 0)
    assert lookup(request, subsection)["source"]["heading"] == "Harbors"
    set_value(request, subsection, -value)
    assert lookup(request, subsection)["priority"] == -value
    assert request("POST", "/clear", {"target": subsection}).json()["priority"] == 0
    assert request("POST", "/clear", {"target": chapter}).json()["priority"] == value
    assert request("POST", "/clear", {"target": doc}).json()["priority"] == 8


def test_priority_scope_and_missing_target_use_404(priority_api):
    request, owner = priority_api
    target = {"kind": "document", "path": BOOK}
    set_value(request, {"kind": "project"}, 42)
    set_value(request, target, 400)
    for headers in (
        {**owner, "X-Memfxxk-User": "other"},
        {**owner, "X-Memfxxk-Project": "other"},
    ):
        hidden = request("POST", "/resolve", target, headers)
        absent = request("POST", "/resolve", {**target, "path": "unknown.md"}, headers)
        assert hidden.status_code == absent.status_code == 404
        assert hidden.json() == absent.json()
        assert lookup(request, {"kind": "project"}, headers)["priority"] == 3
    assert (
        request(
            "POST",
            "/clear",
            {
                "target": {
                    "kind": "section",
                    "path": BOOK,
                    "heading": "Invisible",
                }
            },
        ).status_code
        == 404
    )


def test_record_priority_keeps_old_patch_and_search_with_large_integers(service, make_ctx):
    def access(project):
        ctx = make_ctx(user="u", project=project)
        return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)

    own = access("p")
    record = own.remember(
        RememberRequest(content="lanterns confirm the gate state", scope_drop={"project_id"})
    )
    target = PriorityTarget(kind="record", memory_id=record.memory_id)
    own.patch_memory(record.memory_id, PatchRequest(priority=5, tags=["scene"]))
    assert own.search(SearchRequest(query="lanterns")).hits[0].score.boost == pytest.approx(1.3)
    assert own.set_priority(SetPriority(target=target, priority=-(10**60))).priority == -(10**60)
    assert own.get_memory(record.memory_id).priority == -(10**60)
    assert own.get_memory(record.memory_id).tags == ["scene"]
    assert access("other").get_priority(target).priority == 3
    own.set_priority(SetPriority(target=target, priority=11, scope_drop={"project_id"}))
    unmasked = own.clear_priority(ClearPriority(target=target))
    assert unmasked.priority == 11 and unmasked.source_scope.project_id is None
    assert own.get_memory(record.memory_id).priority == 11


def test_upgrade_round_trip_preserves_old_priorities_and_clamps_new_ones(
    engine, make_migration_config
):
    with engine.begin() as connection:
        config = make_migration_config(connection)
        command.downgrade(config, "0014")
        connection.execute(
            memory_preferences.insert().values(
                id=uuid4(),
                memory_id=uuid4(),
                org_id="migration",
                priority=4,
                tags=["old"],
            )
        )
        command.upgrade(config, "0015")
        expected = connection.execute(
            select(memory_preferences.c.priority, memory_preferences.c.tags)
        ).one()
        assert expected == (4, ["old"])
        for number in (-(10**30), 10**30):
            connection.execute(
                memory_preferences.insert().values(
                    id=uuid4(),
                    memory_id=uuid4(),
                    org_id="migration",
                    priority=number,
                )
            )
        command.downgrade(config, "0014")
        assert sorted(connection.scalars(select(memory_preferences.c.priority))) == [1, 4, 5]
        command.upgrade(config, "head")
