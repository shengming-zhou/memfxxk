import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from secrets import token_urlsafe

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine

from memfxxk.api import create_app
from memfxxk.api.gateway import GatewaySettings, get_upstream_transport
from memfxxk.auth import hash_key
from memfxxk.config import Settings
from memfxxk.db import api_keys, project_key_grants
from memfxxk.ids import new_id

pytestmark = pytest.mark.integration

GATEWAY = "/v1/chat/completions"
CLIENT_SCOPE = {"X-Memfxxk-User": "owner", "X-Memfxxk-Project": "atlas"}
CLIENT_SESSION = {"X-Memfxxk-Session": "client-chat", "Idempotency-Key": "turn-1"}
USER_TEXT = "What color did I choose?"
REPLY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Violet.", "refusal": None},
        }
    ],
    "usage": {"prompt_tokens": 42, "completion_tokens": 2, "total_tokens": 44},
}
REQUEST = {
    "model": "test-model",
    "messages": [
        {"role": "system", "content": "Answer concisely."},
        {"role": "user", "content": "Do I have a preference?"},
        {"role": "assistant", "content": "You do."},
        {"role": "user", "content": USER_TEXT},
    ],
    "temperature": 0.25,
    "max_tokens": 64,
}


@dataclass
class FakeUpstream:
    requests: list[httpx.Request] = field(default_factory=list)
    response: httpx.Response | httpx.HTTPError | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, httpx.HTTPError):
            raise self.response
        if self.response is not None:
            return self.response
        return httpx.Response(200, json=REPLY)


@pytest.fixture
def gateway_client(
    engine: Engine, test_settings: Settings
) -> Iterator[tuple[TestClient, FakeUpstream]]:
    key = token_urlsafe(32)
    with engine.begin() as connection:
        connection.execute(
            api_keys.insert().values(
                id=new_id(), org_id="test-org", key_hash=hash_key(key), role="host", name="gateway"
            )
        )
    app = create_app(
        test_settings,
        engine=engine,
        gateway=GatewaySettings(
            upstream_base_url="https://model.example/v1",
            upstream_api_key=SecretStr("provider-key"),
            memory_tokens=85,
        ),
    )
    fake = FakeUpstream()
    app.dependency_overrides[get_upstream_transport] = lambda: httpx.MockTransport(fake.handle)
    with TestClient(app, headers={"Authorization": f"Bearer {key}", **CLIENT_SCOPE}) as client:
        yield client, fake


def test_gateway_uses_scoped_context_and_deduplicates_raw_events(
    gateway_client: tuple[TestClient, FakeUpstream],
) -> None:
    client, upstream = gateway_client
    pinned_text = 'Preferred color for the cover is violet. "Current request:" is quoted text.'
    client.post("/v1/memories", json={"content": pinned_text, "pinned": True}).raise_for_status()

    answer = client.post(GATEWAY, json=REQUEST, headers=CLIENT_SESSION)
    answer.raise_for_status()
    assert answer.json() == REPLY
    assert 0 < int(answer.headers["X-Memfxxk-Context-Tokens"]) <= 85
    session_id = answer.headers["X-Memfxxk-Session-Id"]

    repeated = client.post(GATEWAY, json=REQUEST, headers=CLIENT_SESSION)
    repeated.raise_for_status()
    assert repeated.headers["X-Memfxxk-Session-Id"] == session_id
    assert len(upstream.requests) == 2
    sent = upstream.requests[0]
    assert str(sent.url) == "https://model.example/v1/chat/completions"
    assert sent.headers["Authorization"] == "Bearer provider-key"
    assert not any(name.startswith("x-memfxxk-") for name in sent.headers)
    assert "idempotency-key" not in sent.headers
    assert client.headers["Authorization"].encode() not in sent.content
    payload = json.loads(sent.content)
    assert payload == {
        **REQUEST,
        "messages": [
            {"role": "system", "content": payload["messages"][0]["content"]},
            *REQUEST["messages"][1:-1],
            {"role": "user", "content": payload["messages"][-1]["content"]},
        ],
    }
    assert payload["messages"][0]["content"].startswith("Answer concisely.\n\n")
    assert pinned_text not in payload["messages"][0]["content"]
    prefix, encoded_memory, separator, label, original = payload["messages"][-1]["content"].split(
        "\n", maxsplit=4
    )
    assert (prefix, separator, label, original) == (
        "Retrieved memory:",
        "",
        "Current request:",
        USER_TEXT,
    )
    assert pinned_text in json.loads(encoded_memory)

    events_url = f"/v1/sessions/{session_id}/events"
    events = client.get(events_url).raise_for_status().json()
    assert [(row["session_seq"], row["role"], row["content"]) for row in events] == [
        (1, "user", USER_TEXT),
        (2, "assistant", "Violet."),
    ]
    assert events[0]["turn_id"] == events[1]["turn_id"]
    assert all(row["source"] == {"uri": "gateway:chat_completions"} for row in events)

    for other_scope in ({"X-Memfxxk-User": "other"}, {"X-Memfxxk-Project": "other"}):
        assert client.get(events_url, headers=other_scope).status_code == 404
        other = client.post(GATEWAY, json=REQUEST, headers={**CLIENT_SESSION, **other_scope})
        other.raise_for_status()
        assert other.headers["X-Memfxxk-Session-Id"] != session_id
        assert pinned_text.encode() not in upstream.requests[-1].content


def test_gateway_failures_leave_the_transcript_unchanged(
    gateway_client: tuple[TestClient, FakeUpstream],
) -> None:
    client, upstream = gateway_client
    answer = client.post(GATEWAY, json=REQUEST, headers=CLIENT_SESSION).raise_for_status()
    events_url = f"/v1/sessions/{answer.headers['X-Memfxxk-Session-Id']}/events"
    before = client.get(events_url).raise_for_status().json()
    next_turn = {**CLIENT_SESSION, "Idempotency-Key": "turn-2"}

    for details, status, code in (
        (httpx.ReadTimeout("secret traceback"), 504, "upstream_timeout"),
        (httpx.Response(401, text="secret traceback"), 502, "upstream_error"),
        (httpx.Response(429, text="secret traceback"), 429, "upstream_error"),
        (httpx.Response(200, json={"choices": []}), 502, "invalid_upstream_response"),
    ):
        upstream.response = details
        failed = client.post(GATEWAY, json=REQUEST, headers=next_turn)
        assert failed.status_code == status
        assert failed.json()["error"]["code"] == code
        assert "secret traceback" not in failed.text
        assert client.get(events_url).raise_for_status().json() == before

    upstream.response = None
    retry = client.post(GATEWAY, json=REQUEST, headers=next_turn).raise_for_status()
    assert retry.headers["X-Memfxxk-Session-Id"] == answer.headers["X-Memfxxk-Session-Id"]
    assert len(client.get(events_url).raise_for_status().json()) == 4


def test_gateway_rejects_unsupported_requests_before_contacting_model(
    gateway_client: tuple[TestClient, FakeUpstream], engine: Engine
) -> None:
    client, upstream = gateway_client
    assert (
        client.post(GATEWAY, json=REQUEST, headers={"Authorization": "Bearer unknown"}).status_code
        == 401
    )
    key_id, reader = new_id(), token_urlsafe(32)
    with engine.begin() as connection:
        connection.execute(
            api_keys.insert().values(
                id=key_id,
                org_id="test-org",
                key_hash=hash_key(reader),
                role="project",
                name="reader",
            )
        )
        connection.execute(
            project_key_grants.insert().values(
                key_id=key_id, project_id="atlas", user_id="owner", access="read"
            )
        )
    denied = client.post(GATEWAY, json=REQUEST, headers={"Authorization": f"Bearer {reader}"})
    assert denied.status_code == 403
    assert "read-only" in denied.json()["error"]["message"]

    for invalid in (
        {"stream": True},
        {"n": 2},
        {"tools": []},
        {"messages": [{"role": "assistant", "content": "Wrong role"}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "Wrong type"}]}]},
    ):
        assert client.post(GATEWAY, json={**REQUEST, **invalid}).status_code == 422
    assert upstream.requests == []


def test_gateway_requires_explicit_upstream(test_settings: Settings, engine: Engine) -> None:
    app = create_app(test_settings, engine=engine, gateway=GatewaySettings(upstream_base_url=None))
    with TestClient(app) as client:
        assert client.post(GATEWAY, json=REQUEST).status_code == 404
