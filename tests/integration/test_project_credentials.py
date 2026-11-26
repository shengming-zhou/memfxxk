import asyncio
import json
from collections.abc import Callable
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import MockTransport, Response
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from typer.testing import CliRunner

from memfxxk.auth import AuthenticationError, authenticate, hash_key
from memfxxk.cli import app
from memfxxk.client import MemfxxkClient
from memfxxk.db import api_keys, events, project_key_grants
from memfxxk.domain import Scope
from memfxxk.ids import derived_id
from memfxxk.mcp_server import make_server

pytestmark = pytest.mark.integration


def _call(api: TestClient, method: str, path: str, key: str | None = None, **kwargs) -> Response:
    request = api.build_request(method, path, **kwargs)
    # Let project credentials supply all three scope headers themselves.
    if "X-Memfxxk-Agent" not in kwargs.get("headers", {}):
        request.headers.pop("X-Memfxxk-Agent", None)
    if key is not None:
        request.headers["Authorization"] = f"Bearer {key}"
    return api.send(request)


def _key(
    engine: Engine, *, project: str, user: str, access: str, agent: str | None = None
) -> tuple[str, str]:
    raw_key, key_id = token_urlsafe(32), uuid4()
    with engine.begin() as conn:
        conn.execute(
            api_keys.insert().values(
                id=key_id,
                org_id="test-org",
                key_hash=hash_key(raw_key),
                role="project",
                name=user,
            )
        )
        conn.execute(
            project_key_grants.insert().values(
                key_id=key_id,
                project_id=project,
                user_id=user,
                agent_id=agent,
                access=access,
            )
        )
    return raw_key, str(key_id)


def test_project_retries_are_scoped_to_the_granted_identity(
    api_client: TestClient, engine: Engine
) -> None:
    def writer(project: str, user: str, agent: str | None = None) -> str:
        return _key(engine, project=project, user=user, agent=agent, access="write")[0]

    alice = writer("west", "alice")
    rotated = writer("west", "alice")
    event = {"kind": "message", "content": "Shared instruction", "idempotency_key": "turn-1"}
    note = {"content": "Shared requirement", "idempotency_key": "note-1"}

    def post(path: str, body: dict, key: str | None) -> Response:
        response = _call(api_client, "POST", path, key, json=body)
        assert response.status_code == 200, response.text
        return response

    [initial] = post("/v1/events", {"events": [event]}, alice).json()
    saved = post("/v1/memories", note, alice).json()
    assert not initial["duplicate"]
    [retry] = post("/v1/events", {"events": [event]}, rotated).json()
    assert retry["duplicate"] and retry["event_id"] == initial["event_id"]
    assert post("/v1/memories", note, rotated).json() == saved

    for credential, visible in (
        (writer("east", "alice"), False),
        (writer("west", "bob"), False),
        (writer("west", "alice", "A"), True),
        (None, False),
    ):
        [independent] = post("/v1/events", {"events": [event]}, credential).json()
        assert not independent["duplicate"] and independent["event_id"] != initial["event_id"]
        independent_note = post("/v1/memories", note, credential).json()
        assert independent_note["memory_id"] != saved["memory_id"]
        for path, resource in (("events", initial["event_id"]), ("memories", saved["memory_id"])):
            denied = _call(api_client, "GET", f"/v1/{path}/{resource}", credential)
            if visible:
                assert denied.status_code == 200  # An agent can view the parent scope.
            else:
                missing = _call(api_client, "GET", f"/v1/{path}/{uuid4()}", credential)
                assert denied.status_code == missing.status_code == 404
                assert denied.json() == missing.json()
    [host] = post("/v1/events", {"events": [event]}, None).json()
    assert host["duplicate"]
    assert (
        _call(api_client, "GET", f"/v1/events/{initial['event_id']}", alice).json()[
            "idempotency_key"
        ]
        == event["idempotency_key"]
    )


def test_mcp_can_retry_ingest_after_project_key_rotation(
    api_client: TestClient, engine: Engine
) -> None:
    writer, _ = _key(engine, project="west", user="alice", access="write")
    rotated, _ = _key(engine, project="west", user="alice", access="write")
    payload = {
        "events": [
            {
                "kind": "message",
                "content": "Retry this turn",
                "role": "user",
                "session_id": "mcp-upload",
                "idempotency_key": "mcp-event-1",
            }
        ]
    }

    async def append(credential: str) -> dict[str, object]:
        with MemfxxkClient(
            str(api_client.base_url), credential, transport=MockTransport(api_client.send)
        ) as client:
            server = make_server(client, enable_host=True)
            async with create_connected_server_and_client_session(server) as session:
                result = await session.call_tool("events_append", payload)
                assert not result.isError and result.structuredContent, result
                [event] = result.structuredContent["results"]
                return event

    first, second = asyncio.run(append(writer)), asyncio.run(append(rotated))
    assert not first["duplicate"] and second["duplicate"]
    assert first["event_id"] == second["event_id"]
    assert first["session_seq"] == second["session_seq"] == 1


def test_workspace_import_uses_project_namespace(api_client: TestClient, engine: Engine) -> None:
    writer, _ = _key(engine, project="west", user="alice", access="write")
    rotated, _ = _key(engine, project="west", user="alice", access="write")
    scope = Scope(org_id="test-org", project_id="west", user_id="alice")
    identity = json.dumps(scope.model_dump(), sort_keys=True)
    lesson_id, failure_id, verification_id = (str(uuid4()) for _ in range(3))
    predictable_keys = [
        f"workspace-lesson:{derived_id('workspace-lesson', identity, lesson_id)}",
        str(derived_id("workspace-event", identity, failure_id)),
        str(derived_id("workspace-event", identity, verification_id)),
    ]
    seeded = _call(
        api_client,
        "POST",
        "/v1/events",
        json={
            "events": [
                {"kind": "message", "content": "Unrelated event", "idempotency_key": key}
                for key in predictable_keys
            ]
        },
    )
    assert seeded.status_code == 200, seeded.text
    source_events = [
        {
            "event_id": event_id,
            "event_time": "2026-01-01T00:00:00Z",
            "payload": {"outcome": outcome},
        }
        for event_id, outcome in ((failure_id, "failure"), (verification_id, "success"))
    ]
    body = {
        "snapshot": {
            "lessons": [
                {
                    "memory_id": lesson_id,
                    "version": 1,
                    "content": "Normalize input before retrying",
                    "failure": source_events[0],
                    "verification": source_events[1],
                }
            ]
        }
    }
    imported = _call(api_client, "POST", "/v1/workspace", writer, json=body)
    assert imported.status_code == 200, imported.text
    assert imported.json()["lessons_created"] == 1
    retry = _call(api_client, "POST", "/v1/workspace", rotated, json=body)
    assert retry.status_code == 200 and retry.json()["lessons_unchanged"] == 1


def test_namespace_downgrade_preserves_events_and_trusted_keys(
    api_client: TestClient,
    engine: Engine,
    make_migration_config: Callable[[Connection], Config],
) -> None:
    writer, _ = _key(engine, project="west", user="alice", access="write")
    payload = {"events": [{"kind": "message", "content": "Keep this", "idempotency_key": "write"}]}
    for key in (None, writer):
        response = _call(api_client, "POST", "/v1/events", key, json=payload)
        assert response.status_code == 200, response.text

    with engine.begin() as conn:
        cfg = make_migration_config(conn)
        command.downgrade(cfg, "0004")
        rows = conn.execute(select(events.c.project_id, events.c.content, events.c.idempotency_key))
        assert set(rows) == {(None, "Keep this", "write"), ("west", "Keep this", None)}
        command.upgrade(cfg, "head")
    retried = _call(api_client, "POST", "/v1/events", json=payload)
    assert retried.status_code == 200 and retried.json()[0]["duplicate"]


def test_project_credentials_share_only_visible_data(
    api_client: TestClient, engine: Engine
) -> None:
    writer, _ = _key(engine, project="warehouse", user="alice", access="write")
    reader, _ = _key(engine, project="warehouse", user="bob", access="read")
    stranger, _ = _key(engine, project="other", user="bob", access="read")
    shared = _call(
        api_client,
        "POST",
        "/v1/memories",
        writer,
        headers={"X-Memfxxk-Actor": '{"type":"user","id":"spoofed"}'},
        json={
            "content": "Warehouse shared delivery convention",
            "scope_drop": ["user_id"],
        },
    )
    assert shared.status_code == 200, shared.text
    path = f"/v1/memories/{shared.json()['memory_id']}"
    memory = _call(api_client, "GET", path, reader).json()
    assert memory["scope"]["project_id"] == "warehouse"
    assert memory["scope"]["user_id"] is None
    fetched_event = _call(api_client, "GET", f"/v1/events/{memory['source_event_ids'][0]}", writer)
    assert fetched_event.json()["actor"] == {"type": "user", "id": "alice"}

    result = _call(
        api_client, "POST", "/v1/memories", writer, json={"content": "Private convention"}
    )
    assert result.status_code == 200
    private = result.json()["memory_id"]
    unavailable = _call(api_client, "GET", f"/v1/memories/{uuid4()}", reader)
    denied = _call(api_client, "GET", f"/v1/memories/{private}", reader)
    other = _call(api_client, "GET", path, stranger)
    assert denied.status_code == other.status_code == unavailable.status_code == 404
    assert denied.json() == other.json() == unavailable.json()
    for header in ("X-Memfxxk-User", "X-Memfxxk-Project", "X-Memfxxk-Agent"):
        assert (
            _call(api_client, "GET", path, reader, headers={header: "spoofed"}).status_code == 403
        )
    matches = _call(
        api_client,
        "POST",
        "/v1/search",
        reader,
        json={
            "query": "convention",
            "channels": ["lexical"],
        },
    )
    assert [hit["memory_id"] for hit in matches.json()["hits"]] == [shared.json()["memory_id"]]

    for action, url, body in (
        ("POST", "/v1/memories", {"content": "Write"}),
        ("POST", "/v1/events", {"events": []}),
        ("PUT", "/v1/documents/write.md", {"content": "Write"}),
        ("POST", "/v1/workspace", {"snapshot": {}}),
        ("PUT", "/v1/sessions/new/state", {"revision": 0}),
        ("PATCH", path, {"priority": 4}),
    ):
        assert _call(api_client, action, url, reader, json=body).status_code == 403


def test_project_writer_cannot_mutate_inherited_resources(
    api_client: TestClient, engine: Engine
) -> None:
    writer, _ = _key(engine, project="warehouse", user="alice", access="write")
    org = _call(api_client, "POST", "/v1/memories", json={"content": "Org-wide note"}).json()
    doc = _call(api_client, "PUT", "/v1/documents/global.md", json={"content": "Org-wide doc"})
    inherited = _call(
        api_client,
        "POST",
        "/v1/events",
        json={
            "events": [
                {"kind": "message", "role": "user", "session_id": "public", "content": "Inherited"},
            ]
        },
    )
    assert doc.status_code == inherited.status_code == 200
    event_id = inherited.json()[0]["event_id"]
    path = f"/v1/memories/{org['memory_id']}"
    assert _call(api_client, "GET", path, writer).status_code == 200
    assert _call(api_client, "GET", "/v1/documents/global.md", writer).status_code == 200
    before = _call(api_client, "GET", "/v1/sessions/public/state", writer).json()

    for method, url, body in (
        ("PATCH", path, {"base_version": 1, "content": "Changed"}),
        ("POST", f"{path}/forget", {"mode": "delete"}),
        ("POST", f"/v1/events/{event_id}/forget", {}),
        ("DELETE", "/v1/documents/global.md", {}),
        ("PUT", "/v1/sessions/public/state", {"revision": before["revision"], "summary": "New"}),
        (
            "POST",
            "/v1/events",
            {
                "events": [
                    {
                        "kind": "message",
                        "role": "user",
                        "session_id": "public",
                        "content": "Injected",
                    },
                ]
            },
        ),
    ):
        assert _call(api_client, method, url, writer, json=body).status_code == 404
    assert _call(api_client, "GET", "/v1/sessions/public/state", writer).json() == before
    for url, body in (
        ("/v1/memories", {"content": "Wider", "scope_drop": ["project_id"]}),
        ("/v1/events", {"events": [{"kind": "message", "scope_drop": ["project_id"]}]}),
    ):
        assert _call(api_client, "POST", url, writer, json=body).status_code == 403
    put = _call(api_client, "PUT", "/v1/documents/new.md", writer, json={"content": "Project doc"})
    assert put.status_code == 200
    own = _call(
        api_client,
        "POST",
        "/v1/events",
        writer,
        json={
            "events": [
                {
                    "kind": "message",
                    "role": "user",
                    "session_id": "own",
                    "content": "Project event",
                },
            ]
        },
    )
    assert own.status_code == 200
    assert _call(api_client, "GET", "/v1/sessions/own/state", writer).json()["last_seq"] == 1

    created = _call(api_client, "POST", "/v1/memories", writer, json={"content": "Draft note"})
    assert created.status_code == 200
    forget = f"/v1/memories/{created.json()['memory_id']}/forget"
    own_event = own.json()[0]["event_id"]
    assert _call(api_client, "POST", forget, writer, json={"mode": "delete"}).status_code == 403
    assert _call(api_client, "POST", f"/v1/events/{own_event}/forget", writer).status_code == 403
    assert _call(api_client, "DELETE", "/v1/documents/new.md", writer).status_code == 403
    assert _call(api_client, "POST", forget, writer, json={"mode": "deactivate"}).status_code == 200


def test_project_credentials_issue_list_and_revoke(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMFXXK_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    runner = CliRunner()
    prefix = ["keys", "create", "--org", "test-org", "--role", "project", "--name", "Bob"]
    assert runner.invoke(app, prefix).exit_code != 0
    issued = runner.invoke(app, [*prefix, "--project", "warehouse", "--user", "bob"])
    assert issued.exit_code == 0, issued.output
    key = issued.stdout.strip()
    grant = authenticate(key, engine=engine).project_grant
    assert grant is not None and grant.access == "read" and grant.user_id == "bob"
    listing = runner.invoke(app, ["keys", "list", "--org", "test-org"])
    [row] = json.loads(listing.stdout)
    assert key not in listing.stdout and "key_hash" not in row
    assert runner.invoke(app, ["keys", "revoke", row["id"], "--org", "other"]).exit_code == 1
    assert runner.invoke(app, ["keys", "revoke", row["id"], "--org", "test-org"]).exit_code == 0
    with pytest.raises(AuthenticationError):
        authenticate(key, engine=engine)


def test_unbound_project_key_fails_closed(engine: Engine) -> None:
    orphan = token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(
            api_keys.insert().values(
                id=uuid4(),
                org_id="test-org",
                key_hash=hash_key(orphan),
                role="project",
                name="orphan",
            )
        )
    with pytest.raises(AuthenticationError):
        authenticate(orphan, engine=engine)


def test_project_key_fails_closed_after_downgrade(
    engine: Engine, make_migration_config: Callable[[Connection], Config]
) -> None:
    key, key_id = _key(engine, project="warehouse", user="bob", access="read")
    with engine.begin() as conn:
        cfg = make_migration_config(conn)
        command.downgrade(cfg, "0002")
        receipt = conn.execute(
            select(api_keys.c.role, api_keys.c.revoked_at).where(api_keys.c.id == key_id)
        ).one()
        assert receipt.role == "host" and receipt.revoked_at is not None
        command.upgrade(cfg, "head")
    with pytest.raises(AuthenticationError):
        authenticate(key, engine=engine)
