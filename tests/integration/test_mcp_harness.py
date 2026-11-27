import asyncio
import json
import socket
import sys
from collections import deque
from collections.abc import Sequence
from threading import Thread

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import StdioServerParameters

from examples.host_loop import Window
from examples.mcp_harness import open_harness
from memfxxk.config import StrategiesSettings
from memfxxk.domain.lessons import FinishTask
from memfxxk.eval.llm import ChatMessage, ChatResult
from memfxxk.service import MemoryService
from memfxxk.text import SimpleCounter

pytestmark = pytest.mark.integration


class ScriptedModel:
    model = "local-scripted-model"

    def __init__(self, *responses: ChatMessage) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[list[ChatMessage], set[str]]] = []

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ChatMessage] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        assert max_tokens == 128
        self.requests.append((list(messages), {tool["function"]["name"] for tool in tools or []}))
        return ChatResult(self.responses.popleft())


def _tool(name: str, arguments: dict[str, object]) -> ChatMessage:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_harness_forwards_tools_and_restores_compaction_after_reconnect(
    api_client: TestClient,
) -> None:
    scope = {"X-Memfxxk-User": "harness-user", "X-Memfxxk-Project": "harness-project"}
    service = api_client.app.state.memory_service
    api_client.app.state.memory_service = MemoryService(
        service.engine,
        service.settings.model_copy(
            update={
                "strategies": StrategiesSettings(blocks="letta"),
            }
        ),
    )
    response = api_client.post(
        "/v1/memory-blocks",
        headers=scope,
        json={
            "label": "handoff",
            "description": "Next handoff",
            "value": "Draft",
            "owner": "agent",
        },
    )
    assert response.status_code == 200, response.text
    block_id = response.json()["id"]
    note = api_client.post(
        "/v1/memories", headers=scope, json={"content": "Cobalt harness release rule"}
    )
    assert note.status_code == 200, note.text
    memory_id = note.json()["memory_id"]
    persona = api_client.put(
        "/v1/documents/persona.md",
        headers=scope,
        json={"content": "The host persona is present on every turn."},
    )
    assert persona.status_code == 200, persona.text

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address, port = listener.getsockname()
        env = {
            "MEMFXXK_URL": f"http://{address}:{port}",
            "MEMFXXK_API_KEY": api_client.headers["Authorization"].removeprefix("Bearer "),
            "MEMFXXK_USER": "harness-user",
            "MEMFXXK_AGENT": "test-agent",
            "MEMFXXK_PROJECT": "harness-project",
            "MEMFXXK_MCP_ENABLE_HOST": "true",
            "MEMFXXK_MCP_ENABLE_MANAGE": "true",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        server = uvicorn.Server(uvicorn.Config(api_client.app, lifespan="off", log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            _assert_harness(api_client, scope, env, memory_id, block_id)
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def _assert_harness(
    api_client: TestClient,
    scope: dict[str, str],
    env: dict[str, str],
    memory_id: str,
    block_id: str,
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from memfxxk.cli import app; app()", "mcp"],
        env=env,
    )
    window = Window(
        limit=4000,
        output=128,
        framing_reserve=64,
        tool_reserve=128,
        working=120,
        memory=900,
        summary=50,
    )
    model = ScriptedModel(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _tool("memory_mark", {"id": memory_id, "mark": "pinned"}),
                _tool("memory_search", {"query": "Cobalt harness"}),
                _tool(
                    "memory_block_edit",
                    {
                        "block_id": block_id,
                        "edit": {
                            "base_revision": 1,
                            "operation": "rewrite",
                            "text": "Thursday handoff",
                        },
                    },
                ),
                _tool("events_append", {"events": [{"kind": "message", "content": "leak"}]}),
            ],
        },
        {"role": "assistant", "content": "Pinned the rule."},
        {"role": "assistant", "content": "The second turn is complete."},
    )
    resumed_model = ScriptedModel(
        {"role": "assistant", "content": "Resumed after reconnect."},
        {"role": "assistant", "content": "Continued with an unfinished tool round."},
    )
    unfinished_events = [
        {
            "kind": "message",
            "role": "user",
            "turn_id": "interrupted",
            "content": "Read this note. " * 120,
        },
        {"kind": "tool_call", "turn_id": "interrupted", "payload": {"tool_call_id": "unresolved"}},
        {"kind": "message", "role": "assistant", "turn_id": "later-1", "content": "First"},
        {"kind": "message", "role": "assistant", "turn_id": "later-2", "content": "Second"},
    ]
    unfinished = api_client.post(
        "/v1/events",
        headers=scope,
        json={"events": [{"session_id": "unfinished", **event} for event in unfinished_events]},
    )
    assert unfinished.status_code == 200, unfinished.text

    async def exercise() -> None:
        async with open_harness(
            parameters, model=model, allow_management=True, window=window, counter=SimpleCounter()
        ) as harness:
            first = await harness.run_turn(
                "cli-harness",
                "Pin the rule. " + "Read the following task details. " * 24,
                turn_id="first",
            )
            assert len(first.events) == 10
            assert first.answer == "Pinned the rule."
            assert "host persona" in first.context.rendered
            assert "memory_mark" in model.requests[0][1]
            assert "memory_block_edit" in model.requests[0][1]
            assert "events_append" not in model.requests[0][1]
            assert "context_build" not in model.requests[0][1]
            for reply in model.requests[1][0][-4:]:
                assert reply["role"] == "tool"
            assert memory_id in model.requests[1][0][-3]["content"]
            assert "Thursday handoff" in model.requests[1][0][-2]["content"]
            assert "unavailable" in model.requests[1][0][-1]["content"]
            second = await harness.run_turn("cli-harness", "What comes next?", turn_id="second")
            assert second.compaction is None
            assert "Cobalt harness release rule" in second.context.rendered
            assert "Thursday handoff" in second.context.rendered
            marked = await harness.finish_task(
                "cli-harness", FinishTask(task="Review the release rule.")
            )
            assert marked.through_seq == second.events[-1].session_seq and not marked.jobs
            assert "session_finish_task" not in model.requests[0][1]

        async with open_harness(
            parameters, model=resumed_model, window=window, counter=SimpleCounter()
        ) as reopened:
            assert "memory_mark" not in reopened.model_tool_names
            assert "memory_block_edit" not in reopened.model_tool_names
            assert "memory_remember" in reopened.model_tool_names
            third = await reopened.run_turn("cli-harness", "Continue the work.", turn_id="third")
            assert third.answer == "Resumed after reconnect."
            assert third.compaction is not None
            assert third.compaction.through_seq == first.events[-1].session_seq
            assert third.context.total_tokens <= window.memory
            assert any(
                item.reason == "rolling_summary"
                for section in third.context.sections
                for item in section.items
            )
            with pytest.raises(ValueError, match="fixed request exceeds"):
                await reopened.run_turn("too-large", "word " * 5000)
            incomplete = await reopened.run_turn("unfinished", "Continue without compacting.")
            assert incomplete.compaction is None
            assert incomplete.answer == "Continued with an unfinished tool round."

    asyncio.run(exercise())
    rows = api_client.get("/v1/sessions/cli-harness/events", headers=scope).json()
    assert len(rows) == 14
    assert {
        row["payload"]["name"]: row["payload"]["outcome"]
        for row in rows
        if row["kind"] == "tool_result"
    } == {
        "memory_mark": "success",
        "memory_search": "success",
        "memory_block_edit": "success",
        "events_append": "failure",
    }
    hidden = {**scope, "X-Memfxxk-Project": "other"}
    assert api_client.get("/v1/sessions/cli-harness/state", headers=hidden).status_code == 404
    assert api_client.get("/v1/sessions/too-large/state", headers=scope).status_code == 404
    state = api_client.get("/v1/sessions/unfinished/state", headers=scope).json()
    assert state["summary_upto_seq"] is None
