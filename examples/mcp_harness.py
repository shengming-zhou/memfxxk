"""Run one model turn with memfxxk over MCP stdio."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import SecretStr, TypeAdapter

from examples.host_loop import Window, _input_tokens
from memfxxk.client._shared import turn_events
from memfxxk.config import EvalModelSettings
from memfxxk.domain.context import (
    BuildContextRequest,
    CommittedCompaction,
    CompactionDraft,
    ContextBundle,
    PrepareCompaction,
)
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.domain.sessions import SessionState
from memfxxk.llm import ChatClient, ChatMessage, ChatModel
from memfxxk.m2.prompts import HOST
from memfxxk.text import TokenCounter, make_token_counter
from memfxxk.tools import TOOL_SPECS

_BASIC_TOOLS = frozenset(spec.name for spec in TOOL_SPECS)
_MANAGEMENT_TOOLS = frozenset(
    ("memory_mark", "memory_link", "memory_set_preferences", "memory_save_lesson")
)
_HOST_TOOLS = (
    "session_state",
    "session_events",
    "context_build",
    "events_append",
    "context_prepare_compaction",
    "context_commit_compaction",
)
_EVENTS = TypeAdapter(list[Event])
_INGEST_RESULTS = TypeAdapter(list[IngestResult])
_DEFER_COMPACTION = frozenset(
    ("incomplete_turn", "incomplete_tool_round", "revision_conflict", "stale_compaction")
)


class HarnessToolError(RuntimeError):
    def __init__(
        self, name: str, message: str, *, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(f"{name}: {message}")
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class HarnessTurn:
    answer: str
    context: ContextBundle
    events: list[IngestResult]
    compaction: CommittedCompaction | None


def _text(result: CallToolResult) -> str:
    return "\n".join(item.text for item in result.content if isinstance(item, TextContent))


async def _discover(client: ClientSession) -> list[Tool]:
    tools: list[Tool] = []
    cursor = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return tools


class MCPHarness:
    def __init__(
        self,
        client: ClientSession,
        tools: list[Tool],
        *,
        model: ChatModel | None = None,
        allow_management: bool = False,
        window: Window = Window(),
        counter: TokenCounter | None = None,
    ) -> None:
        missing = set(_HOST_TOOLS) - {tool.name for tool in tools}
        if missing:
            raise ValueError(f"MCP server needs host tools enabled: {', '.join(sorted(missing))}")
        if (
            min(window.limit, window.output) <= 0
            or min(window.memory, window.working, window.tool_reserve, window.framing_reserve) < 0
        ):
            raise ValueError("Window and output must be positive and reserves cannot be negative")
        if not 1 <= window.summary <= 16_384:
            raise ValueError("Summary tokens must be between 1 and 16384")
        allowed = _BASIC_TOOLS | _MANAGEMENT_TOOLS if allow_management else _BASIC_TOOLS
        self.model_tool_names = frozenset(tool.name for tool in tools if tool.name in allowed)
        self.model_tools: list[ChatMessage] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in tools
            if tool.name in self.model_tool_names
        ]
        self.client = client
        self.model = model
        self.window = window
        self.counter = counter or make_token_counter("tiktoken")

    async def _host_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.client.call_tool(name, arguments)
        data = result.structuredContent
        if result.isError:
            error = data.get("error") if data else None
            if isinstance(error, dict):
                raise HarnessToolError(
                    name, str(error["message"]), status=error.get("status"), code=error.get("code")
                )
            raise HarnessToolError(name, _text(result))
        if data is None:
            raise HarnessToolError(name, "Missing structured result")
        return data

    async def _session_state(self, session_id: str) -> SessionState | None:
        try:
            return SessionState.model_validate(
                await self._host_call("session_state", {"session_id": session_id})
            )
        except HarnessToolError as error:
            if error.status == 404:
                return None
            raise

    async def _compact(
        self, state: SessionState, *, pressured: bool = False
    ) -> CommittedCompaction | None:
        cursor = (state.summary_upto_seq or 0) + 1
        tokens = 0
        closed: list[int] = []
        while cursor <= state.last_seq:
            page = await self._host_call(
                "session_events",
                {"session_id": state.session_id, "from_seq": cursor, "to_seq": state.last_seq},
            )
            events = _EVENTS.validate_python(page["events"])
            if not events:
                break
            for event in events:
                assert event.session_seq is not None
                cursor = event.session_seq + 1
                tokens += self.counter.count(event.content or "")
                tokens += self.counter.count(json.dumps(event.payload, ensure_ascii=False))
                if not event.redacted and event.kind == "message" and event.role == "assistant":
                    closed.append(event.session_seq)
            if (pressured or tokens > self.window.working) and len(closed) >= 2:
                request = PrepareCompaction(
                    session_id=state.session_id,
                    revision=state.revision,
                    through_seq=closed[-2],
                    target_tokens=self.window.summary,
                )
                try:
                    draft = CompactionDraft.model_validate(
                        await self._host_call(
                            "context_prepare_compaction", request.model_dump(mode="json")
                        )
                    )
                    return CommittedCompaction.model_validate(
                        await self._host_call(
                            "context_commit_compaction", {"draft_id": str(draft.draft_id)}
                        )
                    )
                except HarnessToolError as error:
                    if error.code not in _DEFER_COMPACTION:
                        raise
                    return None
        return None

    def _messages(self, user: str, context: str = "") -> list[ChatMessage]:
        return [
            {"role": "system", "content": HOST},
            {
                "role": "user",
                "content": f"Historical evidence:\n{context}\n\nCurrent request:\n{user}",
            },
        ]

    def _tokens(self, messages: list[ChatMessage]) -> int:
        return _input_tokens(self.counter, messages, self.model_tools)

    @property
    def _input_limit(self) -> int:
        return self.window.limit - self.window.output - self.window.framing_reserve

    def _available_context_tokens(self, user: str) -> int:
        empty_tokens = self._tokens(self._messages(user))
        available = min(
            self.window.memory, self._input_limit - self.window.tool_reserve - empty_tokens
        )
        if available < 0:
            raise ValueError("The fixed request exceeds the host context budget")
        return available

    async def _context(self, session_id: str | None, user: str, available: int) -> ContextBundle:
        while True:
            request = BuildContextRequest(
                session_id=session_id,
                query=user,
                max_tokens=available,
                budget={"working": self.window.working},
            )
            context = ContextBundle.model_validate(
                await self._host_call(
                    "context_build", request.model_dump(mode="json", exclude_none=True)
                )
            )
            excess = (
                self._tokens(self._messages(user, context.rendered))
                + self.window.tool_reserve
                - self._input_limit
            )
            if excess <= 0:
                return context
            if available == 0:
                raise ValueError("The assembled request exceeds the host context budget")
            available = max(0, available - excess)

    async def _model_tool(self, name: str, raw_arguments: str) -> tuple[str, bool]:
        if name not in self.model_tool_names:
            return "Error: Tool is unavailable to the model.", False
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            return "Error: Tool arguments must be a JSON object.", False
        if not isinstance(arguments, dict):
            return "Error: Tool arguments must be a JSON object.", False
        result = await self.client.call_tool(name, arguments)
        output = _text(result)
        return output, not result.isError and not output.startswith("Error:")

    async def _complete(self, user: str, context: str) -> tuple[str, list[IngestEvent]]:
        if self.model is None:
            return "Model call skipped. Context built and turn recorded.", []
        messages = self._messages(user, context)
        events: list[IngestEvent] = []
        for _ in range(6):
            if self._tokens(messages) > self._input_limit:
                return "The tool exchange exhausted the context budget.", events
            response = await asyncio.to_thread(
                self.model.complete,
                messages,
                tools=self.model_tools,
                max_tokens=self.window.output,
            )
            assistant = response.message
            calls = assistant.get("tool_calls") or []
            if not calls:
                return assistant.get("content") or "", events
            if len(events) // 2 + len(calls) > 16:
                break
            messages.append(assistant)
            for call in calls:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                call_id = call["id"]
                output, success = await self._model_tool(name, arguments)
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
                events.extend(
                    (
                        IngestEvent(
                            kind="tool_call",
                            role="assistant",
                            payload={"name": name, "args": arguments, "tool_call_id": call_id},
                        ),
                        IngestEvent(
                            kind="tool_result",
                            role="tool",
                            payload={
                                "name": name,
                                "output": output,
                                "tool_call_id": call_id,
                                "outcome": "success" if success else "failure",
                            },
                        ),
                    )
                )
        return "Model tool-call limit reached.", events

    async def run_turn(
        self, session_id: str, user: str, *, turn_id: str | None = None
    ) -> HarnessTurn:
        available = self._available_context_tokens(user)
        state = await self._session_state(session_id)
        context = await self._context(session_id if state is not None else None, user, available)
        compaction = (
            await self._compact(state, pressured=context.pressure == "warning")
            if state is not None
            else None
        )
        if compaction is not None:
            context = await self._context(session_id, user, available)
        answer, tool_events = await self._complete(user, context.rendered)
        events = turn_events(session_id, user, answer, tool_events, turn_id, None)
        stored = await self._host_call(
            "events_append",
            {"events": [event.model_dump(mode="json", exclude_unset=True) for event in events]},
        )
        return HarnessTurn(
            answer=answer,
            context=context,
            events=_INGEST_RESULTS.validate_python(stored["results"]),
            compaction=compaction,
        )


@asynccontextmanager
async def open_harness(
    parameters: StdioServerParameters,
    *,
    model: ChatModel | None = None,
    allow_management: bool = False,
    window: Window = Window(),
    counter: TokenCounter | None = None,
) -> AsyncIterator[MCPHarness]:
    async with (
        stdio_client(parameters) as (receive, send),
        ClientSession(receive, send, read_timeout_seconds=timedelta(seconds=120)) as client,
    ):
        await client.initialize()
        yield MCPHarness(
            client,
            await _discover(client),
            model=model,
            allow_management=allow_management,
            window=window,
            counter=counter,
        )


def _stdio(allow_management: bool) -> StdioServerParameters:
    keys = (
        "MEMFXXK_URL",
        "MEMFXXK_API_KEY",
        "MEMFXXK_USER",
        "MEMFXXK_AGENT",
        "MEMFXXK_PROJECT",
        "NO_PROXY",
    )
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env["MEMFXXK_MCP_ENABLE_HOST"] = "true"
    env["MEMFXXK_MCP_ENABLE_MANAGE"] = "true" if allow_management else "false"
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from memfxxk.cli import app; app()", "mcp"],
        env=env,
    )


async def _run(args: argparse.Namespace, model: ChatModel | None) -> None:
    async with open_harness(
        _stdio(args.manage),
        model=model,
        allow_management=args.manage,
        window=Window(
            limit=args.window_tokens,
            output=args.output_tokens,
            memory=args.memory_tokens,
            working=args.working_tokens,
            summary=args.summary_tokens,
            tool_reserve=args.tool_reserve_tokens,
        ),
    ) as harness:
        turn = await harness.run_turn(args.session, args.message)
        print(turn.answer)


def main() -> None:
    default = Window()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message")
    parser.add_argument("--session", default="mcp-harness-demo")
    parser.add_argument("--manage", action="store_true", help="Offer optional management tools")
    parser.add_argument("--window-tokens", type=int, default=default.limit)
    parser.add_argument("--output-tokens", type=int, default=default.output)
    parser.add_argument("--memory-tokens", type=int, default=default.memory)
    parser.add_argument("--working-tokens", type=int, default=default.working)
    parser.add_argument("--summary-tokens", type=int, default=default.summary)
    parser.add_argument("--tool-reserve-tokens", type=int, default=default.tool_reserve)
    args = parser.parse_args()

    model_name = os.environ.get("MEMFXXK_DEMO_MODEL")
    model_url = os.environ.get("MEMFXXK_DEMO_MODEL_URL")
    if bool(model_name) != bool(model_url):
        parser.error("Set MEMFXXK_DEMO_MODEL and MEMFXXK_DEMO_MODEL_URL together.")
    if model_name and model_url:
        key = os.environ.get("MEMFXXK_DEMO_MODEL_API_KEY")
        settings = EvalModelSettings(
            model=model_name,
            base_url=model_url,
            api_key=SecretStr(key) if key else None,
        )
        with ChatClient(settings, cache_dir=None) as model:
            asyncio.run(_run(args, model))
    else:
        asyncio.run(_run(args, None))


if __name__ == "__main__":
    main()
