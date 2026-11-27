import asyncio
import json
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, GetPromptResult, Prompt, PromptMessage, TextContent, Tool
from pydantic import BaseModel, Field, ValidationError

from memfxxk.client import ClientError
from memfxxk.domain.blocks import EditBlock
from memfxxk.domain.context import BuildContextRequest, CommitCompaction, PrepareCompaction
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.lessons import FinishTask
from memfxxk.domain.manage import LinkRequest, Mark, PatchRequest, SaveLessonRequest
from memfxxk.domain.priorities import ClearPriority, PriorityTarget, SetPriority
from memfxxk.domain.reflect import ReflectRequest
from memfxxk.m2.prompts import HOST
from memfxxk.tools import TOOL_SPECS, HttpBackend, dispatch

if TYPE_CHECKING:
    from memfxxk.client import MemfxxkClient


class _MarkArguments(BaseModel):
    id: UUID
    mark: Mark
    enabled: bool = True


class _EventsArguments(BaseModel):
    events: list[IngestEvent]


class _PreferenceArguments(BaseModel):
    id: UUID
    preferences: PatchRequest


class _SessionArguments(BaseModel):
    session_id: str


class _FinishTaskArguments(_SessionArguments):
    task: FinishTask


class _EventArguments(BaseModel):
    event_id: UUID


class _BlockArguments(BaseModel):
    block_id: UUID


class _BlockListArguments(BaseModel):
    pass


class _BlockEditArguments(_BlockArguments):
    edit: EditBlock


class _SessionEventsArguments(_SessionArguments):
    from_seq: int | None = Field(default=None, gt=0)
    to_seq: int | None = Field(default=None, gt=0)
    limit: int = Field(default=100, gt=0, le=100)


def make_server(
    client: "MemfxxkClient",
    *,
    enable_manage: bool = False,
    enable_host: bool = False,
) -> Server:
    server = Server("memfxxk", instructions=HOST)
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
                Tool(
                    name="memory_set_preferences",
                    description="Apply a user's explicit priority, tags, marks or correction.",
                    inputSchema=_PreferenceArguments.model_json_schema(),
                ),
            ]
        )

    structured_tools = (
        {
            "events_append": (
                "Save original events.",
                _EventsArguments,
                lambda req: {
                    "results": [
                        event.model_dump(mode="json") for event in client.ingest(req.events)
                    ]
                },
            ),
            "session_state": (
                "Read the session boundary.",
                _SessionArguments,
                lambda req: client.get_session(req.session_id).model_dump(mode="json"),
            ),
            "session_finish_task": (
                "Mark a saved task trajectory complete and schedule enabled lesson extraction.",
                _FinishTaskArguments,
                lambda req: client.finish_task(req.session_id, req.task).model_dump(mode="json"),
            ),
            "session_events": (
                "Read an exact session event range.",
                _SessionEventsArguments,
                lambda req: {
                    "events": [
                        event.model_dump(mode="json")
                        for event in client.list_session_events(
                            req.session_id, req.from_seq, req.to_seq, req.limit
                        )
                    ]
                },
            ),
            "event_get": (
                "Read the original event behind a memory reference.",
                _EventArguments,
                lambda req: client.get_event(req.event_id).model_dump(mode="json"),
            ),
            "context_build": (
                "Build memory within a host budget.",
                BuildContextRequest,
                lambda req: client.build_context(req).model_dump(mode="json"),
            ),
            "context_prepare_compaction": (
                "Prepare a bounded summary of closed turns.",
                PrepareCompaction,
                lambda req: client.prepare_compaction(req).model_dump(mode="json"),
            ),
            "context_commit_compaction": (
                "Publish an adopted summary.",
                CommitCompaction,
                lambda req: client.commit_compaction(req.draft_id).model_dump(mode="json"),
            ),
        }
        if enable_host
        else {}
    )
    if enable_manage:
        structured_tools["memory_priority_get"] = (
            "Read a content item's effective integer priority and its source.",
            PriorityTarget,
            lambda req: client.get_priority(req).model_dump(mode="json"),
        )
        structured_tools["memory_priority_set"] = (
            "Set an explicitly approved integer priority on a visible target.",
            SetPriority,
            lambda req: client.set_priority(req).model_dump(mode="json"),
        )
        structured_tools["memory_priority_clear"] = (
            "Clear the priority at the requested scope and show the inherited value.",
            ClearPriority,
            lambda req: client.clear_priority(req).model_dump(mode="json"),
        )
        structured_tools["memory_reflect"] = (
            "Answer from visible memory with verified citations. Opinion writes are opt-in.",
            ReflectRequest,
            lambda req: client.reflect(req).model_dump(mode="json"),
        )
        structured_tools["memory_block_list"] = (
            "List visible memory blocks, their owners, and current revisions.",
            _BlockListArguments,
            lambda _: {"blocks": [item.model_dump(mode="json") for item in client.list_blocks()]},
        )
        structured_tools["memory_block_get"] = (
            "Read a visible memory block and its current revision.",
            _BlockArguments,
            lambda req: client.get_block(req.block_id).model_dump(mode="json"),
        )
        structured_tools["memory_block_edit"] = (
            "Append, replace, or rewrite a block at its current revision. "
            "Protected blocks go to review.",
            _BlockEditArguments,
            lambda req: client.edit_block(req.block_id, req.edit).model_dump(mode="json"),
        )
        structured_tools["memory_save_lesson"] = (
            "Save a user-confirmed lesson with a failed tool result and subsequent verification.",
            SaveLessonRequest,
            lambda req: client.save_lesson(req).model_dump(mode="json"),
        )
    tools.extend(
        Tool(name=name, description=description, inputSchema=request.model_json_schema())
        for name, (description, request, _) in structured_tools.items()
    )

    def call(name: str, arguments: dict[str, object]) -> list[TextContent] | CallToolResult:
        if name in structured_tools:
            _, request, handler = structured_tools[name]
            try:
                result = handler(request.model_validate(arguments))
            except ClientError as exc:
                return _tool_error(exc.code, str(exc), exc.status_code, exc.details)
            except ValidationError:
                return _tool_error("validation_error", "Invalid tool arguments", 422)
            except httpx.RequestError:
                return _tool_error("service_unavailable", "Memory service unavailable", 503)
            return CallToolResult(
                structuredContent=result,
                content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            )
        if enable_manage and name == "memory_mark":
            mark = _MarkArguments.model_validate(arguments)
            request = PatchRequest(**{mark.mark: mark.enabled})
            text = client.patch_memory(mark.id, request).model_dump_json()
        elif enable_manage and name == "memory_link":
            text = client.link(LinkRequest.model_validate(arguments)).model_dump_json()
        elif enable_manage and name == "memory_set_preferences":
            preferences = _PreferenceArguments.model_validate(arguments)
            text = client.patch_memory(preferences.id, preferences.preferences).model_dump_json()
        else:
            text = dispatch(backend, name, arguments)
        return [TextContent(type="text", text=text)]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tools

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return [Prompt(name="memory-use-v1", description="Use and manage remembered evidence.")]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        if name != "memory-use-v1":
            raise ValueError("Unknown prompt")
        return GetPromptResult(
            messages=[
                PromptMessage(role="user", content=TextContent(type="text", text=HOST)),
            ]
        )

    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str, arguments: dict[str, object]
    ) -> list[TextContent] | CallToolResult:
        return await asyncio.to_thread(call, name, arguments)

    return server


def _tool_error(code: str, message: str, status: int, details: object = None) -> CallToolResult:
    result = {"error": {"code": code, "message": message, "status": status, "details": details}}
    return CallToolResult(
        isError=True,
        structuredContent=result,
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
    )


async def serve_stdio(
    client: "MemfxxkClient",
    *,
    enable_manage: bool = False,
    enable_host: bool = False,
) -> None:
    server = make_server(client, enable_manage=enable_manage, enable_host=enable_host)
    async with stdio_server() as (receive, send):
        await server.run(receive, send, server.create_initialization_options())
