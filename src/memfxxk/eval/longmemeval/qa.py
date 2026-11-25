from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from importlib.resources import files
from itertools import batched
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import TYPE_CHECKING, Any, Never
from uuid import UUID

from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.retrieval import MemoryResponse, SearchRequest, SearchResponse
from memfxxk.eval.llm import ChatMessage, ChatModel, ChatResult
from memfxxk.ingest import NotFound
from memfxxk.text import make_token_counter
from memfxxk.tools import ServiceBackend, dispatch, to_openai_tools

from .dataset import Dataset, Question, file_sha256, iter_turns, parse_date
from .ingestion import select_questions
from .metrics import percentile
from .retrieval import CHANNELS, Channel, _commit, _write_json
from .storage import EVAL_PRINCIPAL, eval_context

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService

ANSWER_PROMPT = "answer-v1.txt"
GROUPS = {"G1": "no_memory", "G2": "full_history", "G3": "retrieval_inject", "G4": "agentic_search"}


@dataclass(frozen=True, slots=True)
class QAOptions:
    groups: tuple[str, ...] = ("G1", "G3", "G4")
    k: int = 8
    evidence_budget: int = 1500
    channels: Channel = "hybrid"
    max_tool_calls: int = 6
    max_context_tokens: int = 120_000
    concurrency: int = 4

    def __post_init__(self) -> None:
        if (
            not self.groups
            or len(set(self.groups)) != len(self.groups)
            or any(group not in GROUPS for group in self.groups)
        ):
            raise ValueError("Choose distinct groups from G1, G2, G3, G4")
        if self.channels not in CHANNELS:
            raise ValueError("Channels must be lexical, vector or hybrid")
        if not 1 <= self.k <= 100 or self.concurrency < 1 or self.max_context_tokens < 1:
            raise ValueError("k must be 1–100 and token and concurrency limits must be positive")
        if min(self.evidence_budget, self.max_tool_calls) < 0:
            raise ValueError("Evidence and tool budgets cannot be negative")


def read_prompt(name: str) -> str:
    return files("memfxxk.eval").joinpath("prompts", name).read_text(encoding="utf-8").strip()


def _full_history(question: Question) -> str:
    sessions = zip(question.haystack_dates, question.haystack_sessions, strict=True)
    return "\n\n".join(
        f"Session date: {date}\n" + "\n".join(f"[{m.role}] {m.content}" for m in messages)
        for date, messages in sorted(sessions, key=lambda item: parse_date(item[0]))
    )


class EvaluationBackend:
    def __init__(self, service: BoundService, question: Question, options: QAOptions) -> None:
        self.backend = ServiceBackend(service)
        self.cutoff = question.asked_at
        self.options = options
        self.allowed = {
            turn.memory_id: turn.official_session_id
            for turn in iter_turns(question)
            if turn.event_time <= self.cutoff
        }
        self.retrieved: set[str] = set()
        self.timings: list[dict[str, float]] = []

    def search(self, request: SearchRequest) -> SearchResponse:
        cutoff = min(self.cutoff, request.time_to or self.cutoff)
        if request.time_from is not None and request.time_from > cutoff:
            raise ValueError("The requested start is after the evaluation cutoff")
        capped = request.model_copy(
            update={
                "time_to": cutoff,
                "k": min(request.k, self.options.k),
                "types": ["turn"],
                "channels": list(CHANNELS[self.options.channels]),
                "expand_relations": False,
            }
        )
        response = self.backend.search(capped)
        hits = [hit for hit in response.hits if hit.memory_id in self.allowed]
        self.retrieved.update(self.allowed[hit.memory_id] for hit in hits)
        self.timings.append(response.timings_ms.model_dump())
        return response.model_copy(update={"hits": hits})

    def get(self, id: str, version: int | None = None, window: int = 0) -> MemoryResponse:
        try:
            memory_id = UUID(id)
        except ValueError as error:
            raise NotFound from error
        if memory_id not in self.allowed or version not in (None, 1):
            raise NotFound
        response = self.backend.get(id, version=1, window=window)
        if not isinstance(response, MemoryResponse) or response.event_time > self.cutoff:
            raise NotFound
        neighbors = {}
        self.retrieved.add(self.allowed[memory_id])
        for direction in ("before", "after"):
            allowed_neighbors = [
                item
                for item in getattr(response.neighbors, direction)
                if item.memory_id in self.allowed and item.event_time <= self.cutoff
            ]
            self.retrieved.update(self.allowed[item.memory_id] for item in allowed_neighbors)
            neighbors[direction] = allowed_neighbors
        return response.model_copy(
            update={
                "relations": [],
                "neighbors": response.neighbors.model_copy(update=neighbors),
            }
        )

    def remember(self, *_: object, **__: object) -> Never:
        raise ValueError("Evaluation tools are read-only")


def _answer(result: ChatResult) -> str:
    content = result.message.get("content")
    if result.message.get("tool_calls") or not isinstance(content, str) or not content.strip():
        raise ValueError("The generator did not provide an answer")
    return content.strip()


def _agentic_answer(
    generator: ChatModel,
    messages: list[ChatMessage],
    backend: EvaluationBackend,
    max_calls: int,
) -> tuple[str, list[ChatResult], list[dict[str, Any]], bool]:
    tools = [
        tool
        for tool in to_openai_tools()
        if tool["function"]["name"] in {"memory_search", "memory_get"}
    ]
    calls: list[ChatResult] = []
    actions: list[dict[str, Any]] = []
    while len(actions) < max_calls:
        result = generator.complete(messages.copy(), tools=tools)
        calls.append(result)
        requested = result.message.get("tool_calls") or []
        if not requested:
            return _answer(result), calls, actions, False
        messages.append(result.message)
        for tool in requested:
            function = tool["function"]
            name = function["name"]
            if len(actions) >= max_calls:
                value = "Tool call budget exhausted"
            else:
                action = {"name": name, "query": None}
                actions.append(action)
                try:
                    arguments = json.loads(function["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                    action["query"] = arguments.get("query")
                    if name not in {"memory_search", "memory_get"}:
                        raise ValueError("Unknown evaluation tool")
                    value = dispatch(backend, name, arguments)
                except LookupError:
                    value = "Memory not found"
                except ValueError:
                    value = "Invalid tool request"
            messages.append({"role": "tool", "tool_call_id": tool["id"], "content": value})
    messages.append({"role": "user", "content": "The tool budget has ended. Answer now."})
    result = generator.complete(messages.copy())
    calls.append(result)
    return _answer(result), calls, actions, True


def _generate(
    group: str,
    question: Question,
    service: BoundService,
    options: QAOptions,
    generator: ChatModel,
) -> dict[str, Any]:
    started = perf_counter()
    counter = make_token_counter(service.settings.token_counter)
    row: dict[str, Any] = {
        "group": group,
        "question_id": question.question_id,
        "question_type": question.question_type,
        "abstention": question.abstention,
        "status": "completed",
        "injected_tokens": 0,
        "tool_calls": [],
        "tool_limit_reached": False,
        "retrieval_timings_ms": [],
        "answer_session_retrieved": False,
    }
    history = ""
    if group == "G2":
        history = _full_history(question)
        row["injected_tokens"] = counter.count(history)
    elif group == "G3":
        bundle = service.build_context(
            BuildContextRequest(
                query=question.question,
                sections=["evidence"],
                budget={"evidence": options.evidence_budget},
                k=options.k,
                search_filters=SearchFilters(
                    time_to=question.asked_at,
                    channels=list(CHANNELS[options.channels]),
                    types=["turn"],
                ),
            )
        )
        evidence = bundle.sections[0]
        history = evidence.rendered
        row["injected_tokens"] = evidence.used_tokens
        answer_ids = set(question.answer_session_ids)
        memories = {str(item.ref_id) for item in evidence.items}
        row["answer_session_retrieved"] = any(
            str(turn.memory_id) in memories and turn.official_session_id in answer_ids
            for turn in iter_turns(question)
        )
        if bundle.retrieval_timings_ms is not None:
            row["retrieval_timings_ms"] = [bundle.retrieval_timings_ms.model_dump()]

    prompt = read_prompt(ANSWER_PROMPT).format(date=question.question_date, evidence=history)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": question.question},
    ]
    if group == "G2" and sum(counter.count(message["content"]) for message in messages) > (
        options.max_context_tokens
    ):
        return {**row, "status": "skipped_too_long"}
    if group == "G4":
        backend = EvaluationBackend(service, question, options)
        hypothesis, calls, actions, limited = _agentic_answer(
            generator, messages, backend, options.max_tool_calls
        )
        row["tool_calls"] = actions
        row["tool_limit_reached"] = limited
        row["retrieval_timings_ms"] = backend.timings
        row["answer_session_retrieved"] = bool(
            backend.retrieved.intersection(question.answer_session_ids)
        )
    else:
        calls = [generator.complete(messages)]
        hypothesis = _answer(calls[0])
    row.update(
        hypothesis=hypothesis,
        tool_call_count=len(row["tool_calls"]),
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        network_input_tokens=sum(call.input_tokens for call in calls if not call.cache_hit),
        network_output_tokens=sum(call.output_tokens for call in calls if not call.cache_hit),
        elapsed_ms=(perf_counter() - started) * 1000,
        llm_calls=[
            {key: value for key, value in asdict(call).items() if key != "message"}
            for call in calls
        ],
    )
    return row


def _costs(rows: list[dict[str, Any]], groups: tuple[str, ...]) -> dict[str, Any]:
    names = (
        "injected_tokens",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "network_input_tokens",
        "network_output_tokens",
        "elapsed_ms",
    )
    result = {}
    for group in groups:
        subset = [row for row in rows if row["group"] == group]
        completed = [row for row in subset if row["status"] == "completed"]
        stats: dict[str, Any] = {
            "completed": len(completed),
            "skipped": len(subset) - len(completed),
        }
        for name in (*names, "retrieval_ms"):
            values = [
                sum(call["total"] for call in row["retrieval_timings_ms"])
                if name == "retrieval_ms"
                else row[name]
                for row in completed
            ]
            stats[name] = (
                {
                    "mean": mean(values),
                    "p50": percentile(values, 0.5),
                    "p95": percentile(values, 0.95),
                }
                if values
                else {"mean": None, "p50": None, "p95": None}
            )
        result[group] = stats
    return result


def run_qa(
    service: MemoryService,
    path: Path,
    dataset: Dataset,
    results_dir: Path,
    generator: ChatModel,
    options: QAOptions,
    *,
    limit: int | None = None,
) -> Path:
    digest = file_sha256(path)

    def generate_question(question: Question) -> list[dict[str, Any]]:
        bound = service.for_request(EVAL_PRINCIPAL, eval_context(question.question_id))
        if {"G3", "G4"}.intersection(options.groups) and not bound.eval_has_question(
            dataset, digest
        ):
            raise ValueError(
                f"Question {question.question_id} has not been ingested for this dataset"
            )
        return [_generate(group, question, bound, options, generator) for group in options.groups]

    results_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "kind": "qa",
        "dataset": dataset,
        "dataset_path": str(path.resolve()),
        "sha256": digest,
        "git_commit": _commit(),
        "generator": generator.model,
        "answer_prompt": ANSWER_PROMPT,
        "options": asdict(options),
        "limit": limit,
        "embedding": service.settings.embedding.model_dump(mode="json", exclude={"api_key"}),
        "search": service.settings.search.model_dump(mode="json"),
        "token_counter": service.settings.token_counter,
    }
    _write_json(results_dir / "manifest.json", manifest)
    rows = []
    questions = select_questions(path, limit)
    with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
        for batch in batched(questions, options.concurrency):
            for output in pool.map(generate_question, batch):
                rows.extend(output)
    _write_jsonl(results_dir / "per_question.jsonl", rows)
    for group in options.groups:
        _write_jsonl(
            results_dir / f"hypotheses.{group}.jsonl",
            [
                {"question_id": row["question_id"], "hypothesis": row["hypothesis"]}
                for row in rows
                if row["group"] == group and row["status"] == "completed"
            ],
        )
    _write_json(results_dir / "costs.json", _costs(rows, options.groups))
    (results_dir / "metrics.md").write_text(
        "# LongMemEval QA\n\nWaiting for judging.\n", encoding="utf-8"
    )
    return results_dir


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
