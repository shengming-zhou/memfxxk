import json
from pathlib import Path

import httpx
import pytest

from memfxxk.config import EvalModelSettings, Settings
from memfxxk.eval.llm import ChatClient


def test_t07_chat_cache_hits_without_network(tmp_path: Path) -> None:
    sent = []

    def fake(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "example"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 2},
            },
        )

    config = EvalModelSettings(base_url="https://example.invalid/v1", model="test")
    messages = [{"role": "user", "content": "hello"}]
    with ChatClient(config, tmp_path, httpx.MockTransport(fake)) as client:
        first = client.complete(messages)
    with ChatClient(config, tmp_path, httpx.MockTransport(fake)) as client:
        second = client.complete(messages)
        changed = client.complete(messages, max_tokens=1)
    assert second.cache_hit and not first.cache_hit and not changed.cache_hit
    assert (second.input_tokens, second.output_tokens) == (9, 2)
    assert len(sent) == 2
    assert all(body["temperature"] == 0 for body in sent)


def test_openai_compatible_tool_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "https://example.invalid/provider/v1"
    monkeypatch.setenv("MEMFXXK_EVAL__GEN__BASE_URL", endpoint + "/")
    monkeypatch.setenv("MEMFXXK_EVAL__GEN__MODEL", "fake-chat")
    monkeypatch.setenv("MEMFXXK_EVAL__GEN__API_KEY", "fake-key")
    config = Settings(_env_file=None).eval.gen
    tools = [
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": "Find evidence",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    question = {"role": "user", "content": "When do we release?"}
    tool_request = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_evidence",
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "arguments": '{"query":"release day"}',
                },
            }
        ],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "call_evidence",
        "content": "The release moved to Tuesday.",
    }
    answer = {"role": "assistant", "content": "Tuesday."}
    requests = []

    def fake(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == f"{endpoint}/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-key"
        assert request.headers["Content-Type"] == "application/json"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body == {
                "model": "fake-chat",
                "messages": [question],
                "temperature": 0,
                "tools": tools,
                "max_tokens": 64,
            }
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": tool_request, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 55, "completion_tokens": 18},
                },
            )
        assert body == {
            "model": "fake-chat",
            "messages": [question, tool_request, tool_result],
            "temperature": 0,
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": answer, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 81, "completion_tokens": 4},
            },
        )

    with ChatClient(config, None, httpx.MockTransport(fake)) as client:
        first = client.complete([question], tools=tools, max_tokens=64)
        last = client.complete([question, first.message, tool_result])

    assert len(requests) == 2
    assert first.message == tool_request
    assert last.message == answer
    assert (first.input_tokens, first.output_tokens) == (55, 18)
    assert (last.input_tokens, last.output_tokens) == (81, 4)
    assert (first.retries, last.retries) == (0, 0)


def test_openai_compatible_retries_rate_limits_and_server_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("memfxxk.llm.chat.sleep", delays.append)
    statuses = iter([429, 503, 200])

    def fake(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        status = next(statuses)
        if status == 429:
            return httpx.Response(status, headers={"Retry-After": "3"})
        if status == 503:
            return httpx.Response(status)
        return httpx.Response(
            status,
            json={"choices": [{"message": {"role": "assistant", "content": "answer"}}]},
        )

    config = EvalModelSettings(base_url="https://example.invalid/v1", model="test", max_retries=2)
    with ChatClient(config, None, httpx.MockTransport(fake)) as client:
        result = client.complete([{"role": "user", "content": "hello"}])

    assert delays == [3, 2]
    assert result.retries == 2
    assert result.message["content"] == "answer"
    assert (result.input_tokens, result.output_tokens) == (0, 0)


@pytest.mark.parametrize(("status", "attempts"), [(401, 1), (503, 2)])
def test_openai_compatible_respects_retry_limit(
    monkeypatch: pytest.MonkeyPatch, status: int, attempts: int
) -> None:
    monkeypatch.setattr("memfxxk.llm.chat.sleep", lambda _: None)
    sent = 0

    def fake(_: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(status)

    config = EvalModelSettings(base_url="https://example.invalid/v1", model="test", max_retries=1)
    with (
        ChatClient(config, None, httpx.MockTransport(fake)) as client,
        pytest.raises(httpx.HTTPStatusError) as error,
    ):
        client.complete([{"role": "user", "content": "hello"}])

    assert error.value.response.status_code == status
    assert sent == attempts
