"""Build context, optionally call a configured model, and record one host turn."""

import argparse
import json
import os
from dataclasses import dataclass

import httpx

from memfxxk.client import MemfxxkClient, NotFound
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.sessions import PutSessionState
from memfxxk.m2.prompts import HOST
from memfxxk.text import TokenCounter, make_token_counter


@dataclass(frozen=True, slots=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class Window:
    # The host owns these values, including the margin for provider message framing.
    limit: int = 128_000
    output: int = 4_096
    tool_reserve: int = 2_048
    framing_reserve: int = 256
    memory: int = 6_500
    working: int = 3_000
    summary: int = 600


def _messages(context: str, user: str) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": HOST},
        {"role": "user", "content": f"{context}\n\nCurrent request:\n{user}"},
    ]


def _input_tokens(
    counter: TokenCounter, messages: list[dict[str, object]], tools: list[dict[str, object]]
) -> int:
    return counter.count(
        json.dumps(
            {"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":")
        )
    )


def _compact_if_needed(
    client: MemfxxkClient,
    session_id: str,
    window: Window,
    counter: TokenCounter,
    *,
    pressured: bool = False,
) -> bool:
    state = client.get_session(session_id)
    cursor = (state.summary_upto_seq or 0) + 1
    tokens = 0
    closed: list[int] = []
    while cursor <= state.last_seq:
        page = client.list_session_events(session_id, cursor, state.last_seq)
        if not page:
            break
        for event in page:
            cursor = (event.session_seq or cursor) + 1
            tokens += counter.count(event.content or "") + counter.count(json.dumps(event.payload))
            if event.kind == "message" and event.role == "assistant" and event.session_seq:
                closed.append(event.session_seq)
        if (pressured or tokens > window.working) and len(closed) >= 2:
            draft = client.prepare_compaction(
                PrepareCompaction(
                    session_id=session_id,
                    revision=state.revision,
                    through_seq=closed[-2],
                    target_tokens=window.summary,
                )
            )
            client.commit_compaction(draft.draft_id)
            return True
    return False


def _complete(
    client: MemfxxkClient,
    settings: ModelSettings,
    context: str,
    user: str,
    window: Window,
    counter: TokenCounter,
) -> tuple[str, list[IngestEvent]]:
    specs, handle = client.tools()
    messages = _messages(context, user)
    headers = (
        {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key is not None else {}
    )
    events = []
    with httpx.Client(base_url=settings.base_url.rstrip("/") + "/", headers=headers) as model:
        for _ in range(6):
            if (
                _input_tokens(counter, messages, specs) + window.output + window.framing_reserve
                > window.limit
            ):
                return "The tool exchange exhausted the context budget.", events
            response = model.post(
                "chat/completions",
                json={
                    "model": settings.model,
                    "messages": messages,
                    "tools": specs,
                    "max_tokens": window.output,
                },
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content") or "", events
            messages.append(message)
            for call in calls:
                name = call["function"]["name"]
                raw_arguments = call["function"]["arguments"]
                try:
                    arguments = json.loads(raw_arguments)
                except (TypeError, ValueError):
                    arguments = None
                result = (
                    handle(name, arguments)
                    if isinstance(arguments, dict)
                    else "Error: Tool arguments must be a JSON object."
                )
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                events.extend(
                    [
                        IngestEvent(
                            kind="tool_call",
                            payload={
                                "name": name,
                                "args": raw_arguments,
                                "tool_call_id": call["id"],
                            },
                        ),
                        IngestEvent(
                            kind="tool_result",
                            payload={
                                "name": name,
                                "output": result,
                                "tool_call_id": call["id"],
                            },
                        ),
                    ]
                )
    return "Model tool-call limit reached.", events


def run_turn(
    client: MemfxxkClient,
    session_id: str,
    user: str,
    model: ModelSettings | None = None,
    *,
    window: Window = Window(),
    counter: TokenCounter | None = None,
) -> str:
    try:
        client.get_session(session_id)
    except NotFound:
        client.put_session_state(session_id, PutSessionState(revision=0))
    counter = counter or make_token_counter("tiktoken")
    specs, _ = client.tools()
    fixed = _input_tokens(counter, _messages("", user), specs)
    available = window.limit - window.output - window.tool_reserve - window.framing_reserve - fixed
    if available < 0:
        raise ValueError("The fixed request exceeds the host context budget")
    request = BuildContextRequest(
        session_id=session_id,
        query=user,
        max_tokens=min(window.memory, available),
        budget={"working": window.working},
    )
    bundle = client.build_context(request)
    if _compact_if_needed(
        client, session_id, window, counter, pressured=bundle.pressure == "warning"
    ):
        bundle = client.build_context(request)
    if model is None:
        answer, events = "Model call skipped. Context built and turn recorded.", []
    else:
        answer, events = _complete(client, model, bundle.rendered, user, window, counter)
    client.ingest_turn(session_id, user, answer, tool_events=events)
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message")
    parser.add_argument("--session", default="host-demo")
    parser.add_argument("--url", default=os.environ.get("MEMFXXK_URL", "http://localhost:8000"))
    args = parser.parse_args()
    key = os.environ.get("MEMFXXK_API_KEY")
    if not key:
        parser.error("Set MEMFXXK_API_KEY for the memfxxk service.")
    model_url = os.environ.get("MEMFXXK_DEMO_MODEL_URL")
    model_name = os.environ.get("MEMFXXK_DEMO_MODEL")
    model = (
        ModelSettings(model_url, model_name, os.environ.get("MEMFXXK_DEMO_MODEL_API_KEY"))
        if model_url and model_name
        else None
    )
    with MemfxxkClient(
        args.url,
        key,
        user_id=os.environ.get("MEMFXXK_USER"),
        agent_id=os.environ.get("MEMFXXK_AGENT"),
        project_id=os.environ.get("MEMFXXK_PROJECT"),
    ) as client:
        print(run_turn(client, args.session, args.message, model))


if __name__ == "__main__":
    main()
