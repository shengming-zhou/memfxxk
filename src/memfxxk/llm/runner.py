import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine, and_, func, select

from memfxxk.db import events, llm_calls, transaction
from memfxxk.domain import Scope
from memfxxk.domain.errors import ServiceError
from memfxxk.ids import new_id
from memfxxk.llm.chat import ChatClient, ChatMessage, ChatResult
from memfxxk.llm.settings import DailyLimit, Task, TaskModel
from memfxxk.scope import visible_filter
from memfxxk.text import TokenCounter, make_token_counter

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService

DIMENSIONS = ("user_id", "agent_id", "project_id")


def canonical(messages: Sequence[ChatMessage]) -> str:
    return json.dumps(messages, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class FakeLLM:
    def __init__(self, fixtures: Mapping[Task | tuple[Task, str], Sequence[ChatResult]]) -> None:
        self.fixtures = {key: deque(values) for key, values in fixtures.items()}
        self.calls: list[tuple[Task, str, tuple[ChatMessage, ...]]] = []

    def complete(self, task: Task, messages: Sequence[ChatMessage]) -> ChatResult:
        digest = sha256(canonical(messages).encode()).hexdigest()
        self.calls.append((task, digest, tuple(messages)))
        queue = self.fixtures.get((task, digest), self.fixtures.get(task))
        if not queue:
            raise AssertionError(f"No fake LLM fixture for {task} ({digest})")
        return queue.popleft()


class BudgetExceeded(ServiceError):
    def __init__(self, retry_at: datetime) -> None:
        self.retry_at = retry_at
        super().__init__(
            429,
            "llm_budget_exceeded",
            "Daily memory model budget exhausted",
            {"retry_at": retry_at.isoformat()},
        )


@dataclass(frozen=True, slots=True)
class TaskResult[T]:
    response: ChatResult
    parsed: T | None


@dataclass(frozen=True, slots=True)
class TaskRunner:
    engine: Engine
    config: TaskModel
    tokens: TokenCounter
    limits: tuple[DailyLimit, ...] = ()
    debug: bool = False
    fake: FakeLLM | None = None

    @classmethod
    def for_service(cls, service: "BoundService | MemoryService", task: Task) -> "TaskRunner":
        settings = service.settings
        return cls(
            service.engine,
            settings.task_model(task),
            make_token_counter(settings.token_counter),
            tuple(settings.llm.daily_limits),
            settings.llm.debug_payloads,
            service.fake_llm,
        )

    def _reserve(self, scope: Scope, fields: dict[str, Any], amount: int) -> UUID:
        call_id = new_id()
        matching = [
            limit
            for limit in self.limits
            if limit.org_id == scope.org_id
            and all(getattr(limit, name) in (None, getattr(scope, name)) for name in DIMENSIONS)
        ]
        exceeded = False
        with transaction(self.engine) as conn:
            now = conn.scalar(select(func.now()))
            day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            if matching:
                # All tasks in an organization share the same serialization point for limits.
                conn.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(f"memfxxk-llm:{scope.org_id}", 0)
                        )
                    )
                )
            for limit in matching:
                children = [
                    llm_calls.c[name] == getattr(limit, name)
                    for name in DIMENSIONS
                    if getattr(limit, name) is not None
                ]
                used = conn.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                llm_calls.c.input_tokens
                                + llm_calls.c.output_tokens
                                + llm_calls.c.reserved_tokens
                            ),
                            0,
                        )
                    ).where(
                        and_(
                            llm_calls.c.org_id == scope.org_id,
                            llm_calls.c.created_at >= day,
                            *children,
                        )
                    )
                )
                if used + amount > limit.tokens:
                    exceeded = True
                    break
            conn.execute(
                llm_calls.insert().values(
                    id=call_id,
                    **scope.model_dump(include={"org_id", *DIMENSIONS}),
                    **fields,
                    reserved_tokens=0 if exceeded else amount,
                    status="budget_exceeded" if exceeded else "running",
                    error_code="llm_budget_exceeded" if exceeded else None,
                )
            )
        if exceeded:
            raise BudgetExceeded(day + timedelta(days=1))
        return call_id

    def _finish(
        self,
        call_id: UUID,
        scope: Scope,
        sources: list[UUID],
        prompt: Sequence[ChatMessage],
        started: float,
        response: ChatResult | None,
        error: str | None,
        repair: int,
        estimated: int,
    ) -> None:
        with transaction(self.engine) as conn:
            debug: dict[str, Any] = {}
            if self.debug and sources:
                eligible = set(
                    conn.scalars(
                        select(events.c.id)
                        .where(
                            events.c.id.in_(sources),
                            ~events.c.redacted,
                            visible_filter(events, scope),
                        )
                        .order_by(events.c.id)
                        .with_for_update()
                    )
                )
                if eligible == set(sources):
                    debug = {
                        "debug_input": list(prompt),
                        "debug_output": response.message if response is not None else None,
                    }
            conn.execute(
                llm_calls.update()
                .where(llm_calls.c.id == call_id, visible_filter(llm_calls, scope))
                .values(
                    status="failed" if error else "succeeded",
                    error_code=error,
                    input_tokens=(response.input_tokens or estimated) if response else 0,
                    output_tokens=(
                        response.output_tokens
                        or self.tokens.count(str(response.message.get("content") or ""))
                    )
                    if response
                    else 0,
                    retries=(response.retries if response else 0) + repair,
                    latency_ms=(perf_counter() - started) * 1000,
                    reserved_tokens=0,
                    **debug,
                )
            )

    def complete[T](
        self,
        task: Task,
        scope: Scope,
        messages: Sequence[ChatMessage],
        *,
        prompt_version: str,
        schema: TypeAdapter[T] | None = None,
        request_id: UUID | None = None,
        job_id: UUID | None = None,
        source_event_ids: Sequence[UUID] = (),
        max_tokens: int | None = None,
    ) -> TaskResult[T]:
        if not self.config.model.strip() and self.fake is None:
            raise ServiceError(503, "llm_unconfigured", "Configure a memory model for this task")
        max_output = (
            min(max_tokens, self.config.max_output_tokens)
            if max_tokens
            else self.config.max_output_tokens
        )
        sources = list(dict.fromkeys(source_event_ids))
        request_id = request_id or new_id()
        prompt = list(messages)
        fmt = None
        if schema is not None and self.config.response_format == "json_object":
            fmt = {"type": "json_object"}
        elif schema is not None and self.config.response_format == "json_schema":
            fmt = {
                "type": "json_schema",
                "json_schema": {"name": task, "schema": schema.json_schema()},
            }
        for repair in range(2 if schema is not None else 1):
            encoded = canonical(prompt)
            estimated = self.tokens.count(encoded)
            if fmt is not None:
                estimated += self.tokens.count(json.dumps(fmt, ensure_ascii=False))
            call_id = self._reserve(
                scope,
                {
                    "task": task,
                    "prompt_version": prompt_version,
                    "model": self.config.model or "fake",
                    "request_id": request_id,
                    "job_id": job_id,
                    "source_event_ids": sources,
                    "input_sha256": sha256(encoded.encode()).hexdigest(),
                },
                estimated + max_output,
            )
            started = perf_counter()
            response = None
            try:
                if self.fake is not None:
                    response = self.fake.complete(task, prompt)
                else:
                    with ChatClient(self.config, cache_dir=None) as client:
                        response = client.complete(
                            prompt, max_tokens=max_output, response_format=fmt
                        )
                parsed = schema.validate_json(response.message.get("content")) if schema else None
            except ValidationError as exc:
                self._finish(
                    call_id,
                    scope,
                    sources,
                    prompt,
                    started,
                    response,
                    "llm_invalid_output",
                    repair,
                    estimated,
                )
                if repair or response is None:
                    raise ServiceError(
                        502, "llm_invalid_output", "Model output failed validation"
                    ) from None
                errors = exc.errors(include_input=False, include_context=False, include_url=False)
                prompt = [
                    *prompt,
                    {"role": "assistant", "content": response.message.get("content") or ""},
                    {
                        "role": "user",
                        "content": "Return corrected JSON only. Validation errors: "
                        + json.dumps(errors[:8], ensure_ascii=False, default=str)[:2000],
                    },
                ]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                self._finish(
                    call_id,
                    scope,
                    sources,
                    prompt,
                    started,
                    response,
                    "llm_call_failed",
                    repair,
                    estimated,
                )
                raise ServiceError(502, "llm_call_failed", "Memory model call failed") from exc
            else:
                self._finish(
                    call_id, scope, sources, prompt, started, response, None, repair, estimated
                )
                return TaskResult(response, parsed)
        raise AssertionError("Validation retry did not resolve")


def redact_source_calls(conn: Connection, scope: Scope, sources: Sequence[UUID]) -> None:
    if sources:
        conn.execute(
            llm_calls.update()
            .where(
                llm_calls.c.org_id == scope.org_id,
                llm_calls.c.source_event_ids.overlap(list(sources)),
            )
            .values(debug_input=None, debug_output=None)
        )
