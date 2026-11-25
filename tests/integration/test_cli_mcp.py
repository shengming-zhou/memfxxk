import asyncio
import socket
import sys
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
from memfxxk.domain.sessions import PutSessionState

pytestmark = pytest.mark.integration


def test_cli_and_mcp_over_http(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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
            _check_cli(env)
            asyncio.run(_check_mcp(env))
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def _check_cli(env: dict[str, str]) -> None:
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
