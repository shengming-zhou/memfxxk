import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import BaseModel

from memfxxk.domain.manage import LinkRequest, Mark, PatchRequest
from memfxxk.tools import TOOL_SPECS, HttpBackend, dispatch

if TYPE_CHECKING:
    from memfxxk.client import MemfxxkClient


class _MarkArguments(BaseModel):
    id: UUID
    mark: Mark
    enabled: bool = True


def make_server(client: "MemfxxkClient", *, enable_manage: bool = False) -> Server:
    server = Server("memfxxk")
    backend = HttpBackend(client)
    tools = [
        Tool(name=spec.name, description=spec.description, inputSchema=spec.parameters)
        for spec in TOOL_SPECS
    ]
    if enable_manage:
        tools.extend(
            [
                Tool(
                    name="memory_mark",
                    description="Set or clear an important or pinned mark in the current scope.",
                    inputSchema=_MarkArguments.model_json_schema(),
                ),
                Tool(
                    name="memory_link",
                    description="Link two memories visible in the current scope.",
                    inputSchema=LinkRequest.model_json_schema(),
                ),
            ]
        )

    def call(name: str, arguments: dict[str, object]) -> str:
        if enable_manage and name == "memory_mark":
            mark = _MarkArguments.model_validate(arguments)
            request = PatchRequest(**{mark.mark: mark.enabled})
            return client.patch_memory(mark.id, request).model_dump_json()
        if enable_manage and name == "memory_link":
            return client.link(LinkRequest.model_validate(arguments)).model_dump_json()
        return dispatch(backend, name, arguments)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, object]) -> list[TextContent]:
        response = await asyncio.to_thread(call, name, arguments)
        return [TextContent(type="text", text=response)]

    return server


async def serve_stdio(client: "MemfxxkClient", *, enable_manage: bool = False) -> None:
    server = make_server(client, enable_manage=enable_manage)
    async with stdio_server() as (receive, send):
        await server.run(receive, send, server.create_initialization_options())
