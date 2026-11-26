from secrets import token_urlsafe

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from memfxxk.auth import hash_key
from memfxxk.db import api_keys, project_key_grants
from memfxxk.ids import new_id

pytestmark = pytest.mark.integration


def project_member(engine: Engine, user: str, project: str = "atlas") -> dict[str, str]:
    key, key_id = token_urlsafe(32), new_id()
    with engine.begin() as connection:
        connection.execute(
            api_keys.insert().values(
                id=key_id, org_id="test-org", key_hash=hash_key(key), role="project", name=user
            )
        )
        connection.execute(
            project_key_grants.insert().values(
                key_id=key_id,
                project_id=project,
                user_id=user,
                agent_id="test-agent",
                access="write",
            )
        )
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def shared_pair(api_client: TestClient) -> dict[str, str]:
    ids = [
        api_client.post(
            "/v1/memories",
            headers={"X-Memfxxk-Project": "atlas"},
            json={"content": content, "scope_drop": ["agent_id"]},
        )
        .raise_for_status()
        .json()["memory_id"]
        for content in ("Shipping process", "Receiving process")
    ]
    return {"from_memory_id": ids[0], "to_memory_id": ids[1], "purpose": "related"}


def test_members_can_create_independent_private_relations_between_shared_memories(
    api_client: TestClient, engine: Engine, shared_pair: dict[str, str]
) -> None:
    alice, bob = (project_member(engine, user) for user in ("alice", "bob"))
    other_project = project_member(engine, "alice", "other")
    source = f"/v1/memories/{shared_pair['from_memory_id']}"
    alice_link = api_client.post("/v1/relations", headers=alice, json=shared_pair)
    relation_id = alice_link.raise_for_status().json()["relation_id"]
    assert api_client.get(source, headers=bob).raise_for_status().json()["relations"] == []
    assert (
        api_client.post("/v1/relations", headers=other_project, json=shared_pair).status_code == 404
    )

    denied = api_client.delete(f"/v1/relations/{relation_id}", headers=bob)
    missing = api_client.delete(f"/v1/relations/{new_id()}", headers=bob)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()

    bob_link = api_client.post("/v1/relations", headers=bob, json=shared_pair)
    bob_id = bob_link.raise_for_status().json()["relation_id"]
    assert bob_id != relation_id
    for member, expected_id in ((alice, relation_id), (bob, bob_id)):
        duplicate = api_client.post("/v1/relations", headers=member, json=shared_pair)
        assert duplicate.raise_for_status().json()["relation_id"] == expected_id
        [related] = api_client.get(source, headers=member).raise_for_status().json()["relations"]
        assert related["memory_id"] == shared_pair["to_memory_id"]

    assert api_client.delete(f"/v1/relations/{bob_id}", headers=bob).status_code == 204
    assert api_client.get(source, headers=bob).raise_for_status().json()["relations"] == []
    assert len(api_client.get(source, headers=alice).raise_for_status().json()["relations"]) == 1


def test_member_can_reuse_and_unlink_a_shared_project_relation(
    api_client: TestClient, engine: Engine, shared_pair: dict[str, str]
) -> None:
    project = {"X-Memfxxk-Project": "atlas"}
    original = api_client.post("/v1/relations", headers=project, json=shared_pair)
    relation_id = original.raise_for_status().json()["relation_id"]
    bob = project_member(engine, "bob")
    reused = api_client.post("/v1/relations", headers=bob, json=shared_pair)
    assert reused.raise_for_status().json()["relation_id"] == relation_id
    assert api_client.delete(f"/v1/relations/{relation_id}", headers=bob).status_code == 204
    source = f"/v1/memories/{shared_pair['from_memory_id']}"
    assert api_client.get(source, headers=project).raise_for_status().json()["relations"] == []
