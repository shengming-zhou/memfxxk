"""Run a small, reproducible temporal and multi-session smoke test."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from uuid import UUID

from run_m2_paired import (
    ANSWER_BUDGET,
    MEMORY_BUDGET,
    _isolated_service,
    _latency,
    _load_questions,
    _write,
)
from sqlalchemy import select

from memfxxk.config import Settings, StrategiesSettings
from memfxxk.db import llm_calls
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.reflect import ReflectRequest
from memfxxk.eval.longmemeval.dataset import DATA_DIRECTORY, DATA_FILES, Question, iter_turns
from memfxxk.eval.longmemeval.judging import _grade
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.eval.longmemeval.suite import load_suite
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.prompts import reflect, rerank
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

SUITE = Path("eval/suites/longmemeval-oracle-t16-p3.json")


def evaluate(settings: Settings, question: Question) -> dict:
    turns = [turn for turn in iter_turns(question) if turn.event_time <= question.asked_at]
    messages = [(turn, message) for turn in turns for message in turn.messages]
    with _isolated_service(settings) as database:
        bound = database.for_request(EVAL_PRINCIPAL, eval_context(question.question_id))
        stored = bound.ingest(
            [
                IngestEvent(
                    kind="message",
                    role=message.role,
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    content=message.content,
                    event_time=turn.event_time,
                )
                for turn, message in messages
            ]
        )
        gold = {
            row.event_id
            for (_, message), row in zip(messages, stored, strict=True)
            if message.has_answer
        }
        drain(database, kinds={"embed_record"})
        rows = {}
        for arm in ("hybrid", "hindsight"):
            started = perf_counter()
            if arm == "hybrid":
                bundle = bound.build_context(
                    BuildContextRequest(
                        query=question.question,
                        sections=["evidence"],
                        max_tokens=MEMORY_BUDGET,
                        search_filters=SearchFilters(types=["turn"], query_time=question.asked_at),
                    )
                )
                bundle_ids = [bundle.bundle_id]
                with ChatClient(settings.eval.gen, cache_dir=None) as gen:
                    completion = gen.complete(
                        [
                            {
                                "role": "system",
                                "content": read_prompt(ANSWER_PROMPT).format(
                                    date=question.question_date,
                                    evidence=bundle.rendered,
                                ),
                            },
                            {"role": "user", "content": question.question},
                        ],
                        max_tokens=ANSWER_BUDGET,
                    )
                answer = _answer(completion)
                gen_tokens = completion.input_tokens + completion.output_tokens
                cited = []
            else:
                configuration = database.settings.model_copy(
                    update={
                        "strategies": StrategiesSettings(recall="hindsight", reflect="hindsight"),
                        "search": database.settings.search.model_copy(
                            update={
                                "reranker": "llm",
                                "rerank_candidates": 12,
                            }
                        ),
                        "llm": settings.llm,
                    }
                )
                reflective = MemoryService(database.engine, configuration).for_request(
                    EVAL_PRINCIPAL, bound.ctx
                )
                reply = reflective.reflect(
                    ReflectRequest(
                        query=question.question,
                        query_time=question.asked_at,
                        max_rounds=1,
                        max_tokens=MEMORY_BUDGET,
                    )
                )
                answer, bundle_ids = reply.answer, reply.bundle_ids
                cited = [item.id for item in reply.citations]
                gen_tokens = 0
            latency = (perf_counter() - started) * 1000
            with ChatClient(settings.eval.judge, cache_dir=None) as judge:
                grade = _grade(judge, question, answer)
            items = [
                item
                for bundle_id in bundle_ids
                for section in bound.get_bundle(bundle_id).sections
                for item in section.items
                if item.kind == "memory"
            ]
            loaded = set()
            cited_raw = set()
            for item in items:
                source = bound.get_memory(UUID(item.ref_id), item.version).source_event_ids
                loaded.update(source)
                if UUID(item.ref_id) in cited:
                    cited_raw.update(source)
            with database.engine.connect() as conn:
                calls = conn.execute(
                    select(
                        llm_calls.c.task,
                        llm_calls.c.input_tokens,
                        llm_calls.c.output_tokens,
                        llm_calls.c.status,
                    ).where(visible_filter(llm_calls, bound.ctx))
                ).all()
            rows[arm] = {
                "correct": grade["correct"],
                "answer": answer,
                "latency_ms": latency,
                "gold_loaded": len(loaded & gold),
                "gold_total": len(gold),
                "gold_cited": len(cited_raw & gold),
                "model_tokens": gen_tokens
                + sum(row.input_tokens + row.output_tokens for row in calls),
                "judge_tokens": grade["llm_call"]["input_tokens"]
                + grade["llm_call"]["output_tokens"],
                "model_calls": [dict(row._mapping) for row in calls],
                "references": [item.model_dump(mode="json") for item in items],
            }
    return {"question_id": question.question_id, "type": question.question_type, "arms": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=SUITE)
    args = parser.parse_args()
    settings = Settings()
    args.output.mkdir(parents=True, exist_ok=True)
    dataset = DATA_DIRECTORY / DATA_FILES["oracle"]
    suite = load_suite(args.suite, "oracle", dataset)
    questions = _load_questions(dataset, suite)
    with ThreadPoolExecutor(max_workers=min(settings.eval.concurrency, 3)) as executor:
        results = list(executor.map(lambda item: evaluate(settings, item), questions))
    _write(args.output / "diagnostics.json", results)
    _write(
        args.output / "metrics.json",
        {
            arm: {
                "correct": sum(item["arms"][arm]["correct"] for item in results),
                "gold_loaded": sum(item["arms"][arm]["gold_loaded"] > 0 for item in results),
                "gold_cited": sum(item["arms"][arm]["gold_cited"] > 0 for item in results),
                "total_tokens": sum(
                    item["arms"][arm]["model_tokens"] + item["arms"][arm]["judge_tokens"]
                    for item in results
                ),
                "latency_ms": _latency(item["arms"][arm]["latency_ms"] for item in results),
                "correct_by_type": {
                    kind: sum(
                        item["arms"][arm]["correct"] for item in results if item["type"] == kind
                    )
                    for kind in sorted({item["type"] for item in results})
                },
            }
            for arm in ("hybrid", "hindsight")
        },
    )
    _write(
        args.output / "manifest.json",
        {
            "suite": suite.model_dump(mode="json"),
            "embedding": settings.embedding.model,
            "answer": settings.eval.gen.model,
            "judge": settings.eval.judge.model,
            "reflect": settings.task_model("reflect").model,
            "rerank": settings.task_model("rerank").model,
            "budget": MEMORY_BUDGET,
            "prompts": [reflect.VERSION, rerank.VERSION],
        },
    )
