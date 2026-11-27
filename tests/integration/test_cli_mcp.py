import asyncio
import socket
import sys
from pathlib import Path
from threading import Thread
from uuid import UUID

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from typer.testing import CliRunner

from memfxxk.cli import app
from memfxxk.client import MemfxxkClient
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import SaveLessonRequest
from memfxxk.domain.sessions import PutSessionState

pytestmark = pytest.mark.integration


def test_cli_and_mcp_over_http(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address, port = listener.getsockname()
        env = {
            "MEMFXXK_URL": f"http://{address}:{port}",
            "MEMFXXK_API_KEY": api_client.headers["Authorization"].removeprefix("Bearer "),
            "MEMFXXK_USER": "operator",
            "MEMFXXK_AGENT": "demo-agent",
            "MEMFXXK_PROJECT": "demo-project",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        server = uvicorn.Server(uvicorn.Config(api_client.app, lifespan="off", log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            _check_cli(env, tmp_path)
            asyncio.run(_check_mcp(env))
            asyncio.run(_check_mcp_host(env))
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def _check_cli(env: dict[str, str], tmp_path: Path) -> None:
    runner = CliRunner()

    def run(*args: str) -> str:
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return result.output.strip()

    note = run("remember", "CLI regression cobalt", "--important")
    assert note in run("list", "--important")
    assert "CLI regression cobalt" in run("show", note)
    assert note in run("mark", note, "pinned")
    assert note in run("search", "cobalt", "--json")
    fix = run("remember", "repair cobalt")
    edge = run("link", note, fix)
    UUID(edge)
    run("unlink", edge)
    assert run("doc", "put", "persona.md", "--content", "Prefer small changes") == "1"
    assert run("doc", "show", "persona.md") == "Prefer small changes"
    with MemfxxkClient(
        env["MEMFXXK_URL"],
        env["MEMFXXK_API_KEY"],
        user_id=env["MEMFXXK_USER"],
        agent_id=env["MEMFXXK_AGENT"],
        project_id=env["MEMFXXK_PROJECT"],
    ) as client:
        client.put_session_state("cli-session", PutSessionState(revision=0, state={}))
        failure, verification = client.ingest(
            [
                IngestEvent(kind="tool_result", payload={"outcome": "failure"}),
                IngestEvent(kind="tool_result", payload={"outcome": "success"}),
            ]
        )
        lesson = client.save_lesson(
            SaveLessonRequest(
                title="CLI workspace import",
                scenario="Inventory source rejected",
                steps=["Choose UTF-8"],
                result="Inventory loaded",
                failure_event_id=failure.event_id,
                verification_event_id=verification.event_id,
            )
        )
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "checklist.md").write_text("# Import checklist", encoding="utf-8")
    run("workspace", "import", str(raw))
    archive = tmp_path / "workspace"
    run("workspace", "export", str(archive), "--lesson", str(lesson.memory_id))
    assert (archive / "lessons" / f"{lesson.memory_id}.md").is_file()
    run("--project", "portable-project", "workspace", "import", str(archive))
    assert (
        run("--project", "portable-project", "doc", "show", "checklist.md") == "# Import checklist"
    )
    context = run("context", "cli-session", "cobalt", "--explain")
    assert "Prefer small changes" in context
    assert "pinned " in context and note in context
    cancelled = runner.invoke(app, ["forget", note, "--delete"], input="n\n")
    assert cancelled.exit_code == 1
    assert note in run("list", "--pinned")
    run("forget", note)
    assert note in run("list", "--inactive")


async def _check_mcp(env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from memfxxk.cli import app; app()", "mcp"],
        env={**env, "MEMFXXK_MCP_ENABLE_MANAGE": "false"},
    )
    async with stdio_client(params) as (receive, send), ClientSession(receive, send) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "memory_search",
            "memory_get",
            "memory_remember",
        }
        result = await session.call_tool("memory_remember", {"content": "MCP regression zirconium"})
        assert not result.isError
        assert isinstance(result.content[0], TextContent)
        memory_id = result.content[0].text.split("id=", 1)[1].split()[0]
        UUID(memory_id)
        result = await session.call_tool("memory_search", {"query": "zirconium"})
        assert not result.isError
        assert isinstance(result.content[0], TextContent)
        assert memory_id in result.content[0].text


async def _check_mcp_host(env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from memfxxk.cli import app; app()", "mcp"],
        env={**env, "MEMFXXK_MCP_ENABLE_HOST": "true", "MEMFXXK_MCP_ENABLE_MANAGE": "true"},
    )
    async with stdio_client(params) as (receive, send), ClientSession(receive, send) as session:
        await session.initialize()
        assert "memory-use-v1" in {prompt.name for prompt in (await session.list_prompts()).prompts}

        async def call(name, args):
            result = await session.call_tool(name, args)
            assert not result.isError, result
            assert result.structuredContent is not None
            return result.structuredContent

        session_id = "mcp-host-session"
        sent = await call(
            "events_append",
            {
                "events": [
                    {
                        "kind": "message",
                        "role": "user",
                        "content": "Remember the amber key",
                        "session_id": session_id,
                        "turn_id": "t1",
                    },
                    {
                        "kind": "message",
                        "role": "assistant",
                        "content": "Recorded",
                        "session_id": session_id,
                        "turn_id": "t1",
                    },
                ]
            },
        )
        assert sent["results"][-1]["session_seq"] == 2
        target = {"kind": "record", "memory_id": sent["results"][-1]["memory_ids_touched"][0]}
        saved_priority = await call("memory_priority_set", {"target": target, "priority": -300})
        assert await call("memory_priority_get", target) == saved_priority
        assert saved_priority["priority"] == -300
        assert (await call("memory_priority_clear", {"target": target}))["source"] is None
        state = await call("session_state", {"session_id": session_id})
        assert state["summary_upto_seq"] is None
        draft = await call(
            "context_prepare_compaction",
            {
                "session_id": session_id,
                "revision": state["revision"],
                "through_seq": 2,
                "target_tokens": 100,
            },
        )
        receipt = await call("context_commit_compaction", {"draft_id": draft["draft_id"]})
        assert receipt == await call("context_commit_compaction", {"draft_id": draft["draft_id"]})
        response = await call(
            "context_build",
            {
                "session_id": session_id,
                "max_tokens": 120,
                "sections": ["working"],
            },
        )
        assert response["total_tokens"] <= 120
        assert "amber key" in response["rendered"]
        stored = await call(
            "session_events", {"session_id": session_id, "from_seq": 1, "to_seq": 1}
        )
        assert stored["events"][0]["id"] == draft["source_event_ids"][0]
        outcomes = await call(
            "events_append",
            {
                "events": [
                    {"kind": "tool_result", "payload": {"outcome": "failure"}},
                    {"kind": "tool_result", "payload": {"outcome": "success"}},
                ]
            },
        )
        failed, verified = [item["event_id"] for item in outcomes["results"]]
        lesson = await call(
            "memory_save_lesson",
            {
                "title": "CSV header",
                "scenario": "CSV import has a field mismatch",
                "steps": ["Read the header before choosing the delimiter"],
                "result": "The expected fields were read",
                "failure_event_id": failed,
                "verification_event_id": verified,
            },
        )
        found = await session.call_tool(
            "memory_search", {"query": "CSV header", "types": ["lesson"]}
        )
        assert not found.isError and lesson["memory_id"] in found.content[0].text
        details = await session.call_tool("memory_get", {"id": lesson["memory_id"]})
        assert not details.isError and verified in details.content[0].text
        event = await call("event_get", {"event_id": verified})
        assert event["payload"]["outcome"] == "success"
