"""Prepare evidence and score the frozen three-arm M2 LongMemEval comparison."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from memfxxk.config import M2Settings, Settings, StrategiesSettings
from memfxxk.db import create_engine, llm_calls
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest
from memfxxk.embedding import CachedEmbedder, make_embedding_provider
from memfxxk.eval.longmemeval.dataset import (
    DATA_DIRECTORY,
    DATA_FILES,
    Question,
    Turn,
    iter_questions,
    iter_turns,
)
from memfxxk.eval.longmemeval.judging import _grade
from memfxxk.eval.longmemeval.metrics import percentile
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.eval.longmemeval.suite import QuestionSuite, load_suite
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.llm.settings import LLMSettings, TaskModel
from memfxxk.prompts.compact import VERSION as COMPACTION_PROMPT_VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import BoundService, MemoryService
from memfxxk.text import TokenCounter, make_token_counter

ARMS = ("hybrid", "hybrid-summary", "hybrid-graph")
STRUCTURED_ARM = "hybrid-llm-summary"
SUITE = Path("eval/suites/longmemeval-s-m2-paired30.json")
TOP_K = 8
MEMORY_BUDGET = 1500
ANSWER_BUDGET = 384
SUMMARY_BUDGET = 320
SUMMARY_TARGET = 240
MODEL = "gpt-5.4-mini"
_MIGRATION_LOCK = Lock()
_ENTITY = re.compile(r"\b(?:[A-Z]{2,}[A-Z0-9-]*|[A-Z][a-z]{2,}(?:[-'][A-Za-z]{2,})?)\b")
_COMMON = set(
    (
        "about after also although and are before but can could did does for from had has have "
        "here how however into its just let like looking may might more most need new next now "
        "one our please really should since some thank thanks that the their then there these they "
        "this those through today tomorrow too use want was were what when where which while who "
        "why will with would yes you your yesterday monday tuesday wednesday thursday friday "
        "saturday sunday january february march april june july august september october november "
        "december http https url html ai api assistant user"
    ).split()
)


def _elapsed(started: float) -> float:
    return (perf_counter() - started) * 1000


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_questions(path: Path, suite: QuestionSuite) -> list[Question]:
    remaining = set(suite.question_ids)
    selected = {}
    for question in iter_questions(path):
        if question.question_id in remaining:
            selected[question.question_id] = question
            remaining.remove(question.question_id)
            if not remaining:
                break
    if remaining:
        raise ValueError("Some frozen questions are missing from the dataset")
    return [selected[question_id] for question_id in suite.question_ids]


@contextmanager
def _isolated_service(settings: Settings) -> Iterator[MemoryService]:
    schema = f"m2_eval_{uuid4().hex}"
    database = create_engine(settings.test_database_url)
    with database.begin() as connection:
        connection.execute(CreateSchema(schema))
    url = make_url(settings.test_database_url)
    options = url.query.get("options", "")
    url = url.update_query_dict({"options": f"{options} -csearch_path={schema},public".strip()})
    engine = create_engine(url.render_as_string(hide_password=False))
    try:
        # Alembic uses a process-global operation proxy while a migration runs.
        with _MIGRATION_LOCK, engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            config.attributes["version_table_schema"] = connection.scalar(
                select(func.current_schema())
            )
            command.upgrade(config, "head")
        service = MemoryService(
            engine,
            settings.model_copy(
                update={
                    "m2": M2Settings(),
                    "llm": LLMSettings(),
                    "strategies": StrategiesSettings(),
                }
            ),
        )
        service.ensure_index()
        yield service
    finally:
        engine.dispose()
        with database.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        database.dispose()


def _replay(
    bound: BoundService, turns: list[Turn], session_id: str
) -> tuple[dict[UUID, Turn], dict[UUID, str], list[int]]:
    staged = [(turn, message) for turn in turns for message in turn.messages]
    stored = bound.ingest(
        [
            IngestEvent(
                kind="message",
                role=message.role,
                content=message.content,
                session_id=session_id,
                turn_id=turn.turn_id,
                event_time=turn.event_time,
            )
            for turn, message in staged
        ]
    )
    by_memory, gold_events, closes = {}, {}, []
    for (turn, message), item in zip(staged, stored, strict=True):
        by_memory[item.memory_ids_touched[0]] = turn
        if message.has_answer:
            gold_events[item.event_id] = message.content
        if message.role == "assistant" and item.session_seq is not None:
            closes.append(item.session_seq)
    return by_memory, gold_events, closes


def _edges(turns: dict[UUID, Turn]) -> Iterator[tuple[UUID, UUID, str]]:
    mentions: dict[str, list[UUID]] = defaultdict(list)
    for memory_id, turn in turns.items():
        names = {
            match.casefold()
            for message in turn.messages
            if message.role == "user"
            for match in _ENTITY.findall(message.content)
        }
        for name in sorted(names - _COMMON):
            mentions[name].append(memory_id)

    pairs: set[frozenset[UUID]] = set()
    degree: Counter[UUID] = Counter()
    for name, nodes in sorted(mentions.items(), key=lambda item: (len(item[1]), item[0])):
        if not 2 <= len(nodes) <= 8:
            continue
        for index, node in enumerate(nodes):
            for other in reversed(nodes[:index]):
                if turns[node].session_id == turns[other].session_id:
                    continue
                pair = frozenset((other, node))
                if pair not in pairs and degree[node] < 4 and degree[other] < 4:
                    pairs.add(pair)
                    degree[node] += 1
                    degree[other] += 1
                    yield other, node, name
                break


def _summary(
    bound: BoundService,
    session_id: str,
    closes: list[int],
    *,
    gold_events: dict[UUID, str] | None = None,
) -> tuple[str, int]:
    if not closes:
        return "", 0
    for through in sorted({closes[len(closes) // 2], closes[-1]}):
        state = bound.get_session(session_id)
        draft = bound.prepare_compaction(
            PrepareCompaction(
                session_id=session_id,
                revision=state.revision,
                through_seq=through,
                target_tokens=SUMMARY_TARGET,
            )
        )
        bound.commit_compaction(draft.draft_id)
    bundle = bound.build_context(
        BuildContextRequest(
            session_id=session_id,
            sections=["working"],
            max_tokens=SUMMARY_BUDGET,
            budget={"working": SUMMARY_BUDGET},
            host_history_from_seq=1,
            auto_recall=False,
        )
    )
    cited_gold = set()
    if gold_events and draft.checkpoint and _annotated_in(bundle.rendered, draft.summary):
        cited_gold = {
            event_id
            for field in type(draft.checkpoint).model_fields
            for claim in getattr(draft.checkpoint, field)
            for event_id in claim.source_event_ids
            if event_id in gold_events
        }
    return bundle.rendered, len(cited_gold)


def _annotated_in(text: str, content: str) -> bool:
    return bool(content.strip()) and escape(content.strip(), quote=False) in text


def _arm(
    bound: BoundService,
    question: Question,
    arm: str,
    turns: dict[UUID, Turn],
    gold_events: dict[UUID, str],
    counter: TokenCounter,
    embedding_ms: float,
    summary: str = "",
    *,
    graph_evidence_tokens: int = 0,
) -> dict[str, Any]:
    started = perf_counter()
    prefix = f"{summary}\n\n" if summary else ""
    remaining = MEMORY_BUDGET - counter.count(prefix)
    request = BuildContextRequest(
        query=question.question,
        sections=["evidence"],
        k=TOP_K,
        max_tokens=remaining,
        graph_evidence_tokens=graph_evidence_tokens,
        budget={"evidence": remaining},
        search_filters=SearchFilters(
            time_to=question.asked_at,
            types=["turn"],
            graph_hops=2 if arm == "hybrid-graph" else 0,
        ),
    )
    bundle = bound.build_context(request)
    evidence = prefix + bundle.rendered
    if counter.count(evidence) > MEMORY_BUDGET:
        request.max_tokens -= counter.count(evidence) - MEMORY_BUDGET + 2
        bundle = bound.build_context(request)
        evidence = prefix + bundle.rendered
    tokens = counter.count(evidence)
    if tokens > MEMORY_BUDGET:
        raise RuntimeError("Memory budget exceeded")
    packing_ms = _elapsed(started)
    section = bundle.sections[0]
    candidate_ids = {UUID(item.ref_id) for item in section.items + section.dropped}
    loaded_ids = {UUID(item.ref_id) for item in section.items}
    gold_turn_ids = {key for key, turn in turns.items() if turn.has_answer}
    timings = bundle.retrieval_timings_ms
    retrieval_ms = timings.total if timings else 0
    # Measure the same cold question embedding separately for every arm.
    memory_ms = packing_ms - (timings.embed if timings else 0) + embedding_ms
    return {
        "arm": arm,
        "history": evidence,
        "memory_tokens": tokens,
        "memory_ms": memory_ms,
        "retrieval_ms": retrieval_ms + embedding_ms,
        "graph_ms": timings.graph if timings else 0,
        "graph_candidates": sum(
            item.reason.startswith("graph(") for item in section.items + section.dropped
        ),
        "graph_loaded": sum(item.reason.startswith("graph(") for item in section.items),
        "summary_tokens": counter.count(summary),
        "gold_turn_total": len(gold_turn_ids),
        "gold_turn_recalled": len(gold_turn_ids & candidate_ids),
        "gold_turn_loaded": len(gold_turn_ids & loaded_ids),
        "gold_messages_total": len(gold_events),
        "gold_messages_full": sum(_annotated_in(evidence, text) for text in gold_events.values()),
        "gold_messages_full_summary": sum(
            _annotated_in(summary, text) for text in gold_events.values()
        ),
        "gold_sessions_total": len(set(question.answer_session_ids)),
        "gold_sessions_recalled": len(
            set(question.answer_session_ids)
            & {turns[key].official_session_id for key in candidate_ids}
        ),
        "gold_sessions_loaded": len(
            set(question.answer_session_ids)
            & {turns[key].official_session_id for key in loaded_ids}
        ),
        "candidate_turns": [
            turns[UUID(item.ref_id)].turn_id for item in section.items + section.dropped
        ],
        "loaded_turns": [turns[UUID(item.ref_id)].turn_id for item in section.items],
        "gold_turns": [turns[key].turn_id for key in gold_turn_ids],
    }


def _prepare_question(
    service: MemoryService,
    query: Question,
    *,
    graph_evidence_tokens: int = 0,
    structured: TaskModel | None = None,
) -> dict[str, Any]:
    bound = service.for_request(EVAL_PRINCIPAL, eval_context(query.question_id))
    session_id = f"m2:{query.question_id}"
    counter = make_token_counter(service.settings.token_counter)
    turns = sorted(
        (turn for turn in iter_turns(query) if turn.event_time <= query.asked_at),
        key=lambda turn: turn.event_time,
    )
    started = perf_counter()
    mapped, gold, closes = _replay(bound, turns, session_id)
    ingest_ms = _elapsed(started)
    started = perf_counter()
    drain(service, kinds={"embed_record"}, max_rounds=1_000)
    embed_index_ms = _elapsed(started)
    total, embedded = bound.eval_evidence_coverage(query.asked_at)
    if total != len(turns) or embedded != len(turns):
        raise RuntimeError(f"{query.question_id}: {embedded}/{len(turns)} turns have real vectors")
    embedder = CachedEmbedder(service.engine, make_embedding_provider(service.settings))
    started = perf_counter()
    embedder.embed([query.question])
    embedding_ms = _elapsed(started)
    base = _arm(bound, query, ARMS[0], mapped, gold, counter, embedding_ms)

    started = perf_counter()
    summary, _ = _summary(bound, session_id, closes)
    summary_ms = _elapsed(started)
    compact = _arm(bound, query, ARMS[1], mapped, gold, counter, embedding_ms, summary)
    compact["compaction_method"] = "extractive_fallback"

    if structured is not None:
        llm_settings = service.settings.model_copy(
            update={
                "llm": LLMSettings(default=structured),
            }
        )
        llm_service = MemoryService(service.engine, llm_settings)
        isolated = bound.ctx.model_copy(update={"user_id": "structured-compaction"})
        llm_bound = llm_service.for_request(EVAL_PRINCIPAL, isolated)
        llm_session = f"{session_id}:llm"
        _, llm_gold, llm_closes = _replay(llm_bound, turns, llm_session)
        started = perf_counter()
        llm_summary, cited_gold = _summary(llm_bound, llm_session, llm_closes, gold_events=llm_gold)
        llm_arm = _arm(
            bound, query, STRUCTURED_ARM, mapped, gold, counter, embedding_ms, llm_summary
        )
        with service.engine.connect() as conn:
            usage = conn.execute(
                select(
                    func.count(),
                    func.sum(llm_calls.c.input_tokens),
                    func.sum(llm_calls.c.output_tokens),
                ).where(visible_filter(llm_calls, isolated), llm_calls.c.task == "compact")
            ).one()
        llm_arm.update(
            compaction_method="llm",
            compaction_ms=_elapsed(started),
            compaction_calls=usage[0],
            compaction_input_tokens=usage[1] or 0,
            compaction_output_tokens=usage[2] or 0,
            gold_messages_cited_summary=cited_gold,
        )
        return {
            "question_id": query.question_id,
            "question_type": query.question_type,
            "turn_count": total,
            "embedding_ms": embedding_ms,
            "ingest_ms": ingest_ms,
            "embed_index_ms": embed_index_ms,
            "summary_build_ms": summary_ms,
            "graph_build_ms": 0,
            "graph_edges": 0,
            "arms": [base, compact, llm_arm],
        }

    started = perf_counter()
    links = list(_edges(mapped))
    for source, target, entity in links:
        bound.link(
            LinkRequest(
                from_memory_id=source,
                to_memory_id=target,
                purpose=f"shared entity: {entity}",
            )
        )
    graph_ms = _elapsed(started)
    graph = _arm(
        bound,
        query,
        ARMS[2],
        mapped,
        gold,
        counter,
        embedding_ms,
        graph_evidence_tokens=graph_evidence_tokens,
    )
    return {
        "question_id": query.question_id,
        "question_type": query.question_type,
        "turn_count": total,
        "embedding_ms": embedding_ms,
        "ingest_ms": ingest_ms,
        "embed_index_ms": embed_index_ms,
        "summary_build_ms": summary_ms,
        "graph_build_ms": graph_ms,
        "graph_edges": len(links),
        "arms": [base, compact, graph],
    }


def prepare(
    settings: Settings,
    output: Path,
    suite: QuestionSuite,
    questions: list[Question],
    *,
    graph_evidence_tokens: int = 0,
    structured_summary: bool = False,
    workers: int = 1,
) -> None:
    if settings.embedding.provider != "openai_compatible" or settings.token_counter != "tiktoken":
        raise ValueError("Use a real OpenAI-compatible embedding endpoint and the tiktoken counter")
    if not 0 <= graph_evidence_tokens <= MEMORY_BUDGET:
        raise ValueError(f"Graph evidence tokens must be from 0 to {MEMORY_BUDGET}")
    manifest = {
        "protocol": "m2-paired-v1",
        **suite.model_dump(mode="json"),
        "embedding": {"model": settings.embedding.model, "dim": settings.embedding.dim},
        "answer_model": MODEL,
        "judge_model": MODEL,
        "top_k": TOP_K,
        "memory_budget": MEMORY_BUDGET,
        "answer_budget": ANSWER_BUDGET,
        "summary_budget": SUMMARY_BUDGET,
        "summary_target": SUMMARY_TARGET,
        "summary": "Two local extractive compactions on the chronological replay",
        "graph": "Two hops over cross-session links from rare names in raw user messages",
        "graph_build_uses_question_or_gold": False,
        "arms": list(ARMS),
    }
    structured = settings.task_model("compact") if structured_summary else None
    if structured_summary:
        if structured is None or not structured.model:
            raise ValueError("Configure the compact task model for a structured summary run")
        manifest.update(
            protocol="t16-p1-paired-v1",
            arms=[*ARMS[:2], STRUCTURED_ARM],
            compaction_model=structured.model,
            compaction_prompt=COMPACTION_PROMPT_VERSION,
            compaction_max_output=structured.max_output_tokens,
            compaction_response_format=structured.response_format,
            prepare_workers=workers,
            answer_model=settings.eval.gen.model or MODEL,
            judge_model=settings.eval.judge.model or MODEL,
        )
    if graph_evidence_tokens:
        manifest["graph_evidence_tokens"] = graph_evidence_tokens
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise ValueError("The output directory contains a different experiment")
    _write(manifest_path, manifest)
    path = output / "prepared.jsonl"
    done = {row["question_id"] for row in _read_rows(path)}
    if len(done) == len(questions):
        return
    remaining = [question for question in questions if question.question_id not in done]
    checkpoint = Lock()

    def prepare_batch(batch: list[Question]) -> None:
        with _isolated_service(settings) as service:
            for question in batch:
                row = _prepare_question(
                    service,
                    question,
                    graph_evidence_tokens=graph_evidence_tokens,
                    structured=structured,
                )
                with checkpoint:
                    _append(path, row)
                    done.add(question.question_id)
                    print(
                        f"Prepared {len(done)}/{len(questions)} · {question.question_id} · "
                        f"{row['turn_count']} turns, {row['graph_edges']} edges",
                        flush=True,
                    )

    if workers == 1:
        prepare_batch(remaining)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            batches = [
                remaining[index::workers] for index in range(workers) if index < len(remaining)
            ]
            for _ in executor.map(prepare_batch, batches):
                pass


def score(settings: Settings, output: Path, questions: list[Question]) -> None:
    gen = settings.eval.gen
    judge = settings.eval.judge if settings.eval.judge.model else gen
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    arms = manifest["arms"]
    if (gen.model, judge.model) != (manifest["answer_model"], manifest["judge_model"]):
        raise ValueError("Configured answer and judge models must match the run manifest")
    prepared = _read_rows(output / "prepared.jsonl")
    if {row["question_id"] for row in prepared} != {question.question_id for question in questions}:
        raise ValueError("Complete evidence preparation before scoring")
    question_by_id = {question.question_id: question for question in questions}
    path = output / "answers.jsonl"
    generation_path = output / "generations.jsonl"
    done = {(row["question_id"], row["arm"]) for row in _read_rows(path)}
    saved_generations = {
        (row["question_id"], row["arm"]): row for row in _read_rows(generation_path)
    }
    checkpoint = Lock()
    with (
        ChatClient(gen, cache_dir=None) as answerer,
        ChatClient(judge, cache_dir=None) as grader,
        ThreadPoolExecutor(max_workers=settings.eval.concurrency) as executor,
    ):

        def evaluate(row: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any]:
            question = question_by_id[row["question_id"]]
            key = (question.question_id, arm["arm"])
            generation = saved_generations.get(key)
            if generation is None:
                prompt = read_prompt(ANSWER_PROMPT).format(
                    date=question.question_date,
                    evidence=arm["history"],
                )
                guess = answerer.complete(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": question.question},
                    ],
                    max_tokens=ANSWER_BUDGET,
                )
                generation = {
                    "question_id": key[0],
                    "arm": key[1],
                    "answer": _answer(guess),
                    "answer_ms": guess.latency_ms,
                    "answer_input_tokens": guess.input_tokens,
                    "answer_output_tokens": guess.output_tokens,
                }
                with checkpoint:
                    _append(generation_path, generation)
            grade = _grade(grader, question, generation["answer"])
            return {
                **generation,
                "correct": grade["correct"],
                "judge_ms": grade["llm_call"]["latency_ms"],
                "judge_input_tokens": grade["llm_call"]["input_tokens"],
                "judge_output_tokens": grade["llm_call"]["output_tokens"],
            }

        for name in arms:
            work = [
                (row, arm)
                for row in prepared
                for arm in row["arms"]
                if arm["arm"] == name and (row["question_id"], name) not in done
            ]
            if not work:
                continue

            def save(result: dict[str, Any]) -> None:
                _append(path, result)
                done.add((result["question_id"], result["arm"]))
                print(f"Scored {len(done)}/{len(questions) * len(arms)} · {name}", flush=True)

            # Validate the live gateway with the first scored case before issuing a batch.
            save(evaluate(*work[0]))
            for result in executor.map(lambda item: evaluate(*item), work[1:]):
                save(result)


def _evidence_stage(row: dict[str, Any]) -> str:
    if row["gold_messages_full"]:
        return "complete_gold_message_loaded"
    if row["gold_turn_loaded"]:
        return "gold_turn_loaded_but_message_partial"
    if row["gold_turn_recalled"]:
        return "gold_turn_recalled_but_not_loaded"
    return "no_gold_turn_recalled"


def _latency(values: Iterable[float]) -> dict[str, float]:
    data = list(values)
    return {f"p{p}": round(percentile(data, p / 100), 2) for p in (50, 95)} if data else {}


def report(output: Path) -> None:
    prepared = _read_rows(output / "prepared.jsonl")
    manifest_path = output / "manifest.json"
    arms = (
        json.loads(manifest_path.read_text(encoding="utf-8"))["arms"]
        if manifest_path.exists()
        else ARMS
    )
    answers = {
        (row["question_id"], row["arm"]): row for row in _read_rows(output / "answers.jsonl")
    }
    generations = {
        (row["question_id"], row["arm"]): row for row in _read_rows(output / "generations.jsonl")
    }
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "question_count": len(prepared),
        "turn_count": sum(row["turn_count"] for row in prepared),
        "graph_edges": sum(row["graph_edges"] for row in prepared),
        "ingest_total_ms": round(sum(row["ingest_ms"] for row in prepared), 2),
        "index_total_ms": round(sum(row["embed_index_ms"] for row in prepared), 2),
        "summary_build_ms": _latency(row["summary_build_ms"] for row in prepared),
        "graph_build_ms": _latency(row["graph_build_ms"] for row in prepared),
        "groups": {},
    }
    diagnostics = []
    for row in prepared:
        for arm in row["arms"]:
            question_id, name = row["question_id"], arm["arm"]
            diagnostics.append(
                {
                    "question_id": question_id,
                    "question_type": row["question_type"],
                    **{key: value for key, value in arm.items() if key != "history"},
                    "evidence_stage": _evidence_stage(arm),
                    **{
                        key: value
                        for key, value in generations.get((question_id, name), {}).items()
                        if key != "answer"
                    },
                    **{
                        key: value
                        for key, value in answers.get((question_id, name), {}).items()
                        if key != "answer"
                    },
                }
            )
    for name in arms:
        rows = [row for row in diagnostics if row["arm"] == name]
        scored = [row for row in rows if "correct" in row]
        generated = [row for row in rows if "answer_ms" in row]
        group = {
            "prepared": len(rows),
            "generated": len(generated),
            "scored": len(scored),
            "correct": sum(row["correct"] for row in scored) if scored else None,
            "evidence_stages": Counter(row["evidence_stage"] for row in rows),
            "recall_any": sum(bool(row["gold_turn_recalled"]) for row in rows),
            "recall_all": sum(
                row["gold_turn_recalled"] == row["gold_turn_total"] > 0 for row in rows
            ),
            "loaded_any": sum(bool(row["gold_turn_loaded"]) for row in rows),
            "gold_messages_full": sum(row["gold_messages_full"] for row in rows),
            "complete_gold_message_questions": sum(bool(row["gold_messages_full"]) for row in rows),
            "all_gold_messages_questions": sum(
                row["gold_messages_full"] == row["gold_messages_total"] > 0 for row in rows
            ),
            "wrong_with_all_gold_messages": sum(
                not row["correct"] and row["gold_messages_full"] == row["gold_messages_total"] > 0
                for row in scored
            ),
            "complete_gold_message_summary_questions": sum(
                bool(row["gold_messages_full_summary"]) for row in rows
            ),
            "graph_candidates_questions": sum(bool(row["graph_candidates"]) for row in rows),
            "graph_loaded_questions": sum(bool(row["graph_loaded"]) for row in rows),
            "memory_tokens": sum(row["memory_tokens"] for row in rows),
            "memory_ms": _latency(row["memory_ms"] for row in rows),
            "retrieval_ms": _latency(row["retrieval_ms"] for row in rows),
            "answer_ms": _latency(row["answer_ms"] for row in generated),
            "end_to_end_ms": _latency(row["memory_ms"] + row["answer_ms"] for row in generated),
            "judge_ms": _latency(row["judge_ms"] for row in scored),
            "wrong_by_evidence_stage": Counter(
                row["evidence_stage"] for row in scored if not row["correct"]
            ),
            "by_type": {
                category: {
                    "scored": sum(row["question_type"] == category for row in scored),
                    "correct": sum(
                        row["question_type"] == category and row["correct"] for row in scored
                    ),
                    "complete_gold_message_questions": sum(
                        row["question_type"] == category and bool(row["gold_messages_full"])
                        for row in rows
                    ),
                }
                for category in sorted({row["question_type"] for row in rows})
            },
        }
        for phase in ("answer", "judge"):
            calls = generated if phase == "answer" else scored
            for kind in ("input", "output"):
                key = f"{phase}_{kind}_tokens"
                group[key] = sum(row[key] for row in calls) if calls else None
        if name == STRUCTURED_ARM:
            for key in ("compaction_calls", "compaction_input_tokens", "compaction_output_tokens"):
                group[key] = sum(row[key] for row in rows)
            group["compaction_ms"] = _latency(row["compaction_ms"] for row in rows)
            group["gold_message_cited_summary_questions"] = sum(
                bool(row.get("gold_messages_cited_summary", 0)) for row in rows
            )
        result["groups"][name] = group
    by_arm = {
        name: {row["question_id"]: row for row in diagnostics if row["arm"] == name}
        for name in arms
    }
    base = by_arm[arms[0]]
    result["paired_vs_hybrid"] = {}
    for name in arms[1:]:
        candidate = by_arm[name]
        paired = [(row, candidate[key]) for key, row in base.items() if key in candidate]
        graded = [(a, b) for a, b in paired if "correct" in a and "correct" in b]
        result["paired_vs_hybrid"][name] = {
            "scored_pairs": len(graded),
            "answer_improved": sum(b["correct"] and not a["correct"] for a, b in graded),
            "answer_regressed": sum(a["correct"] and not b["correct"] for a, b in graded),
            "gold_turn_recall_gained": sum(
                bool(b["gold_turn_recalled"]) and not a["gold_turn_recalled"] for a, b in paired
            ),
            "gold_turn_recall_lost": sum(
                bool(a["gold_turn_recalled"]) and not b["gold_turn_recalled"] for a, b in paired
            ),
            "complete_evidence_gained": sum(
                bool(b["gold_messages_full"]) and not a["gold_messages_full"] for a, b in paired
            ),
            "complete_evidence_lost": sum(
                bool(a["gold_messages_full"]) and not b["gold_messages_full"] for a, b in paired
            ),
        }
    if STRUCTURED_ARM in by_arm:
        structured = by_arm[STRUCTURED_ARM]
        paired = [
            (row, structured[key]) for key, row in by_arm[ARMS[1]].items() if key in structured
        ]
        graded = [
            (local, model) for local, model in paired if "correct" in local and "correct" in model
        ]
        result["paired_llm_vs_extractive"] = {
            "scored_pairs": len(graded),
            "answer_improved": sum(
                model["correct"] and not local["correct"] for local, model in graded
            ),
            "answer_regressed": sum(
                local["correct"] and not model["correct"] for local, model in graded
            ),
        }
    (output / "diagnostics.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in diagnostics),
        encoding="utf-8",
    )
    _write(output / "metrics.json", result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "score", "report", "all"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument(
        "--graph-evidence-tokens",
        type=int,
        default=0,
        help="Reserve evidence tokens in the graph arm at preparation time.",
    )
    parser.add_argument(
        "--structured-summary",
        action="store_true",
        help="Compare model compaction to hybrid and local extractive compaction.",
    )
    parser.add_argument(
        "--prepare-workers",
        type=int,
        default=1,
        help="Replay batches in separate database schemas with structured compaction.",
    )
    args = parser.parse_args()
    if not 0 <= args.graph_evidence_tokens <= MEMORY_BUDGET:
        parser.error(f"--graph-evidence-tokens must be from 0 to {MEMORY_BUDGET}")
    if args.graph_evidence_tokens and args.phase not in ("prepare", "all"):
        parser.error("--graph-evidence-tokens requires the prepare or all phase")
    if args.structured_summary and (
        args.graph_evidence_tokens or args.phase not in ("prepare", "all")
    ):
        parser.error(
            "--structured-summary is a separate preparation experiment without graph tokens"
        )
    if args.prepare_workers < 1 or (args.prepare_workers != 1 and not args.structured_summary):
        parser.error("--prepare-workers must be positive and requires --structured-summary")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.phase == "report":
        report(args.output)
        return
    settings = Settings()
    source = DATA_DIRECTORY / DATA_FILES["s"]
    suite = load_suite(args.suite, "s", source)
    questions = _load_questions(source, suite)
    try:
        if args.phase in ("prepare", "all"):
            prepare(
                settings,
                args.output,
                suite,
                questions,
                graph_evidence_tokens=args.graph_evidence_tokens,
                structured_summary=args.structured_summary,
                workers=args.prepare_workers,
            )
        if args.phase in ("score", "all"):
            score(settings, args.output, questions)
    finally:
        report(args.output)


if __name__ == "__main__":
    main()
