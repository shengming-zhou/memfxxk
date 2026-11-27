import pytest

from memfxxk.context.feedback import replay
from memfxxk.domain.feedback import AllocationChange, AllocationState

pytestmark = pytest.mark.integration
HEADERS = {"X-Memfxxk-User": "writer", "X-Memfxxk-Project": "harbor"}
ROUTE = "/v1/context/allocation"
PATH = "rules.md"
RULE = "Bright scarlet signals close the north gate before dawn."


@pytest.fixture
def author(api_client):
    response = api_client.put(
        f"/v1/documents/{PATH}",
        headers=HEADERS,
        json={"content": f"# Rule\n{RULE}", "load_policy": "always"},
    )
    assert response.status_code == 200
    identity = response.json()["document_id"]
    return identity, {"kind": "document", "ref_id": identity, "section_index": 0}


def build(api_client, budget, *, learn=False):
    response = api_client.post(
        "/v1/context/build",
        headers=HEADERS,
        json={"allocator": "adaptive", "max_tokens": budget, "learn_from_feedback": learn},
    )
    assert response.status_code == 200, response.text
    return response.json()


def send(api_client, bundle, *, target=None, signal=None, assistant=None, headers=HEADERS):
    return api_client.post(
        ROUTE + "/feedback",
        headers=headers,
        json={
            "bundle_id": bundle["bundle_id"],
            "observations": [{"signal": signal, "target": target}] if target else [],
            "assistant_event_id": assistant,
        },
    )


def assistant_event(api_client, *, retrieved=False, answer="The new task is complete."):
    common = {"session_id": "fiction", "turn_id": "observed"}
    events = [{"kind": "message", "role": "user", "content": "Continue writing", **common}]
    if retrieved:
        events.extend(
            [
                {
                    "kind": "tool_call",
                    "role": "assistant",
                    "payload": {"name": "memory_get"},
                    **common,
                },
                {
                    "kind": "tool_result",
                    "role": "tool",
                    **common,
                    "payload": {
                        "name": "memory_get",
                        "outcome": "success",
                        "output": f"doc:{PATH} revision=1\n{RULE}",
                    },
                },
            ]
        )
    events.append({"kind": "message", "role": "assistant", "content": answer, **common})
    response = api_client.post("/v1/events", json={"events": events}, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()[-1]["event_id"]


def test_retrieved_same_turn_is_detected_and_idempotent(api_client, author):
    bundle = build(api_client, 0)
    assistant = assistant_event(api_client, retrieved=True)
    first = send(api_client, bundle, assistant=assistant)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1 and first.json()["multipliers"]["3"] > 1
    assert send(api_client, bundle, assistant=assistant).json() == first.json()
    assert build(api_client, 400, learn=True)["allocation"]["feedback_version"] == 1
    assert build(api_client, 400)["allocation"]["feedback_version"] is None


@pytest.mark.parametrize("used", [False, True])
def test_unused_has_text_and_citation_protection(api_client, author, used):
    bundle = build(api_client, 220)
    assistant = assistant_event(api_client, answer=RULE if used else "I will adjust the character.")
    response = send(api_client, bundle, assistant=assistant)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == (0 if used else 1)
    if not used:
        assert response.json()["multipliers"]["3"] < 1


def test_conflict_and_user_correction_are_logged_and_replayable(api_client, author):
    _, target = author
    start = AllocationState.model_validate(api_client.get(ROUTE, headers=HEADERS).json())
    bundle = build(api_client, 0)
    for name in ("conflict", "correction"):
        response = send(api_client, bundle, target=target, signal=name)
        assert response.status_code == 200, response.text
    final = AllocationState.model_validate(response.json())
    assert final.version == 2 and final.multipliers[3] > 1.03
    rows = api_client.get(ROUTE + "/history", headers=HEADERS).json()
    changes = [AllocationChange.model_validate(row) for row in rows]
    assert [change.signals[0].signal for change in changes] == ["conflict", "correction"]
    assert replay(start, changes) == final
    assert RULE not in str(rows)


def test_scope_isolation_writes_and_invalid_target(api_client, author):
    _, target = author
    bundle = build(api_client, 0)
    assert send(api_client, bundle, target=target, signal="correction").status_code == 200
    for other in (
        {**HEADERS, "X-Memfxxk-User": "another"},
        {**HEADERS, "X-Memfxxk-Project": "another"},
    ):
        assert api_client.get(ROUTE, headers=other).json()["version"] == 0
        assert api_client.get(ROUTE + "/history", headers=other).json() == []
        assert (
            send(api_client, bundle, target=target, signal="correction", headers=other).status_code
            == 404
        )
    unknown = {**target, "ref_id": "missing"}
    assert send(api_client, bundle, target=unknown, signal="correction").status_code == 404


def test_reset_preserves_versions_and_is_a_replayable_boundary(api_client, author):
    _, target = author
    bundle = build(api_client, 0)
    assert send(api_client, bundle, target=target, signal="conflict").status_code == 200
    state = api_client.post(ROUTE + "/reset", headers=HEADERS).json()
    assert state["version"] == 2 and state["multipliers"] == {}
    after = api_client.get(ROUTE + "/history?after=1", headers=HEADERS).json()
    assert len(after) == 1 and after[0]["reset"]
