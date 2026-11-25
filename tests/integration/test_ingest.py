from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from memfxxk.db import events, jobs, records
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.events import IngestEvent
from memfxxk.ids import derived_id
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration

OWNER_HEADERS = {"X-Memfxxk-User": "u1", "X-Memfxxk-Project": "p1"}


def post_events(api_client: TestClient, *items: dict[str, object]) -> list[dict[str, Any]]:
    response = api_client.post("/v1/events", headers=OWNER_HEADERS, json={"events": items})
    response.raise_for_status()
    return response.json()


def test_repeated_idempotency_key_reuses_event(api_client: TestClient, engine: Engine) -> None:
    event = {
        "kind": "message",
        "role": "user",
        "content": "Remember the release notes",
        "session_id": "releases",
        "idempotency_key": "releases-first-event",
    }

    [first] = post_events(api_client, event)
    [duplicate] = post_events(api_client, event)

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["event_id"] == first["event_id"]
    assert duplicate["session_seq"] == first["session_seq"] == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(events)) == 1
        assert connection.scalar(select(func.count()).select_from(jobs)) == 1


def test_concurrent_sessions_allocate_contiguous_sequences(
    service: MemoryService, make_ctx: Callable[..., RequestContext], engine: Engine
) -> None:
    ctx = make_ctx(user="u1", project="p1")
    bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    ready = Barrier(2, timeout=30)

    def write_batch(writer: str) -> list[int | None]:
        batch = [
            IngestEvent(
                kind="message",
                role="user",
                content=f"Event {index} from {writer}",
                session_id="parallel",
            )
            for index in range(50)
        ]
        ready.wait()
        return [result.session_seq for result in bound.ingest(batch)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write_batch, "first")
        second = executor.submit(write_batch, "second")
        assert set(first.result(timeout=60) + second.result(timeout=60)) == set(range(1, 101))

    with engine.connect() as connection:
        sequence = connection.scalars(
            select(events.c.session_seq).order_by(events.c.session_seq)
        ).all()
    assert sequence == list(range(1, 101))


def test_concurrent_retry_reuses_event_without_consuming_sequence(
    service: MemoryService, make_ctx: Callable[..., RequestContext]
) -> None:
    ctx = make_ctx(user="u1", project="p1")
    bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    event = IngestEvent(
        kind="message",
        role="user",
        content="Save once",
        session_id="race",
        idempotency_key="race-1",
    )
    ready = Barrier(2, timeout=30)

    def write_once() -> tuple[UUID, int | None, bool]:
        ready.wait()
        [result] = bound.ingest([event])
        return result.event_id, result.session_seq, result.duplicate

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write_once)
        second = executor.submit(write_once)
        event_a, seq_a, duplicate_a = first.result(timeout=60)
        event_b, seq_b, duplicate_b = second.result(timeout=60)

    assert event_a == event_b
    assert seq_a == seq_b == 1
    assert {duplicate_a, duplicate_b} == {True, False}
    [next_event] = bound.ingest([event.model_copy(update={"idempotency_key": "race-2"})])
    assert next_event.session_seq == 2


def test_second_ingest_rebuilds_existing_turn(api_client: TestClient, engine: Engine) -> None:
    same_turn = {"session_id": "build", "turn_id": "turn-a"}
    [user] = post_events(
        api_client,
        {
            **same_turn,
            "kind": "message",
            "role": "user",
            "content": "Repair the build",
            "event_time": "2026-01-06T12:01:00Z",
        },
    )
    with engine.connect() as connection:
        first_version = connection.execute(select(records)).mappings().one()

    tool, assistant = post_events(
        api_client,
        {
            **same_turn,
            "kind": "tool_result",
            "content": "Dependency installed",
            "payload": {"name": "build"},
            "event_time": "2026-01-06T12:00:00Z",
        },
        {**same_turn, "kind": "message", "role": "assistant", "content": "Build repaired"},
    )
    with engine.connect() as connection:
        turn = connection.execute(select(records)).mappings().one()
        embedding_job = (
            connection.execute(select(jobs).where(jobs.c.status == "queued")).mappings().one()
        )

    assert turn["id"] == first_version["id"]
    assert turn["memory_id"] == derived_id("turn", "test-org", "build", "turn-a")
    assert turn["version"] == 1
    assert turn["generation"] == first_version["generation"] + 1
    content = turn["content"]
    assert content.index("Repair the build") < content.index("Dependency installed")
    assert content.index("Dependency installed") < content.index("Build repaired")
    assert turn["event_time"].isoformat() == "2026-01-06T12:00:00+00:00"
    assert turn["source_event_ids"] == [
        UUID(result["event_id"]) for result in (user, tool, assistant)
    ]
    assert turn["source_pos"] == {"session_id": "build", "seq_from": 1, "seq_to": 3}
    assert embedding_job["payload"] == {
        "record_id": str(turn["id"]),
        "generation": turn["generation"],
    }


def test_tool_output_is_truncated_in_turn_and_preserved_in_event(
    api_client: TestClient, engine: Engine
) -> None:
    output = "HEAD" + "a" * 1600 + "REMOVE_THIS_MIDDLE" + "b" * 1600 + "TAIL"
    [result] = post_events(
        api_client,
        {
            "kind": "tool_result",
            "content": output,
            "payload": {"name": "diagnostics"},
            "session_id": "diagnostics",
        },
    )

    with engine.connect() as connection:
        turn_text = connection.execute(select(records.c.content)).scalar_one()
    assert "HEAD" in turn_text
    assert "TAIL" in turn_text
    assert "REMOVE_THIS_MIDDLE" not in turn_text
    response = api_client.get(f"/v1/events/{result['event_id']}", headers=OWNER_HEADERS)
    response.raise_for_status()
    assert response.json()["content"] == output


@pytest.mark.parametrize(
    "other_headers",
    [
        {**OWNER_HEADERS, "X-Memfxxk-User": "u2"},
        {**OWNER_HEADERS, "X-Memfxxk-Project": "p2"},
    ],
)
def test_ingest_and_reads_enforce_user_and_project_scope(
    api_client: TestClient, other_headers: dict[str, str]
) -> None:
    private_event = {
        "kind": "message",
        "role": "user",
        "content": "Private",
        "session_id": "private",
        "idempotency_key": "private-key",
    }
    [result] = post_events(api_client, private_event)
    path = "/v1/sessions/private/events"
    response = api_client.get(path, headers=OWNER_HEADERS)
    response.raise_for_status()
    [visible] = response.json()
    assert visible["id"] == result["event_id"]
    assert api_client.get(path, headers=other_headers).status_code == 404
    assert (
        api_client.get(f"/v1/events/{result['event_id']}", headers=other_headers).status_code == 404
    )

    attempt = {**private_event, "idempotency_key": "different-key"}
    assert (
        api_client.post("/v1/events", headers=other_headers, json={"events": [attempt]}).status_code
        == 404
    )
    assert (
        api_client.post(
            "/v1/events", headers=other_headers, json={"events": [private_event]}
        ).status_code
        == 404
    )


def test_session_event_range_limits_and_preserves_order(api_client: TestClient) -> None:
    inserted = post_events(
        api_client,
        *(
            {"kind": "message", "role": "user", "content": f"Message {i}", "session_id": "range"}
            for i in range(5)
        ),
    )
    response = api_client.get(
        "/v1/sessions/range/events?from_seq=2&to_seq=4&limit=2", headers=OWNER_HEADERS
    )
    response.raise_for_status()
    assert [event["id"] for event in response.json()] == [
        result["event_id"] for result in inserted[1:3]
    ]


def test_shared_session_still_filters_private_events(api_client: TestClient) -> None:
    base = {"kind": "message", "role": "user", "session_id": "shared"}
    shared, private = post_events(
        api_client,
        {**base, "content": "Shared release", "scope_drop": ["user_id"]},
        {**base, "content": "Private release task"},
    )

    teammate = {**OWNER_HEADERS, "X-Memfxxk-User": "u2"}
    response = api_client.get("/v1/sessions/shared/events", headers=teammate)
    response.raise_for_status()
    assert [item["id"] for item in response.json()] == [shared["event_id"]]
    assert api_client.get(f"/v1/events/{private['event_id']}", headers=teammate).status_code == 404


def test_import_without_session_adds_only_raw_event(api_client: TestClient, engine: Engine) -> None:
    [result] = post_events(
        api_client,
        {
            "kind": "document_import",
            "content": "Release checklist",
            "source": {"uri": "docs/release.md"},
        },
    )

    assert result["session_seq"] is None
    assert result["memory_ids_touched"] == []
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(events)) == 1
        assert connection.scalar(select(func.count()).select_from(records)) == 0
        assert connection.scalar(select(func.count()).select_from(jobs)) == 0
