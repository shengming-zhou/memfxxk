"""Compare independently replayed fact strategies against M1 G3 on frozen Oracle histories."""

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from uuid import UUID

from run_m2_paired import (
    ANSWER_BUDGET,
    MEMORY_BUDGET,
    TOP_K,
    _append,
    _isolated_service,
    _latency,
    _load_questions,
    _read_rows,
    _write,
)
from sqlalchemy import select

from memfxxk.config import Settings, StrategiesSettings
from memfxxk.db import jobs, llm_calls
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.eval.longmemeval.dataset import DATA_DIRECTORY, DATA_FILES, Question, iter_turns
from memfxxk.eval.longmemeval.judging import _grade
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.eval.longmemeval.suite import load_suite
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.prompts.extract import ADDITIVE_VERSION, CLASSIC_VERSION
from memfxxk.prompts.reconcile import VERSION as RECONCILE_VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

SUITE = Path("eval/suites/longmemeval-oracle-t16-p2.json")
ARMS = ("G3", "mem0_classic", "mem0_additive")


def evaluate(settings: Settings, question: Question) -> dict:
    turns = sorted(
        (turn for turn in iter_turns(question) if turn.event_time <= question.asked_at),
        key=lambda turn: turn.event_time,
    )
    staged = [(turn, message) for turn in turns for message in turn.messages]
    results = []
    with _isolated_service(settings) as database:
        for arm in ARMS:
            derived = arm != "G3"
            arm_settings = database.settings.model_copy(
                update={
                    "llm": settings.llm,
                    "strategies": StrategiesSettings(
                        facts=arm if derived else "off",
                        mem0_additive_ranking=arm == "mem0_additive",
                    ),
                }
            )
            service = MemoryService(database.engine, arm_settings)
            scope = eval_context(f"{question.question_id}:{arm}")
            bound = service.for_request(EVAL_PRINCIPAL, scope)
            start_build = perf_counter()
            stored = bound.ingest(
                [
                    IngestEvent(
                        kind="message",
                        session_id=f"{arm}:{turn.session_id}",
                        turn_id=turn.turn_id,
                        content=message.content,
                        role=message.role,
                        event_time=turn.event_time,
                    )
                    for turn, message in staged
                ]
            )
            gold = {
                item.event_id
                for (_, message), item in zip(staged, stored, strict=True)
                if message.has_answer
            }
            drain(service, kinds={"embed_record", "extract_facts"}, max_rounds=1000)
            build_ms = (perf_counter() - start_build) * 1000
            with service.engine.connect() as conn:
                usage = conn.execute(
                    select(
                        llm_calls.c.status, llm_calls.c.input_tokens, llm_calls.c.output_tokens
                    ).where(visible_filter(llm_calls, scope))
                ).all()
                job_statuses = conn.scalars(
                    select(jobs.c.status).where(
                        jobs.c.kind == "extract_facts",
                        jobs.c.payload["scope"]["project_id"].as_string() == scope.project_id,
                    )
                ).all()
            if any(status != "done" for status in job_statuses):
                raise RuntimeError(
                    f"{question.question_id} {arm} unfinished {Counter(job_statuses)}"
                )
            total, embedded = bound.eval_evidence_coverage(question.asked_at)
            if total != len(turns) or embedded != len(turns):
                raise RuntimeError(
                    f"{question.question_id} {arm} only {embedded}/{len(turns)} turns embedded"
                )
            start = perf_counter()
            bundle = bound.build_context(
                BuildContextRequest(
                    query=question.question,
                    sections=["evidence"],
                    k=TOP_K,
                    max_tokens=MEMORY_BUDGET,
                    budget={"evidence": MEMORY_BUDGET},
                    search_filters=SearchFilters(
                        time_to=question.asked_at,
                        as_of_valid=question.asked_at,
                        types=["turn", "fact"] if derived else ["turn"],
                    ),
                )
            )
            memory_ms = (perf_counter() - start) * 1000
            prompt = read_prompt(ANSWER_PROMPT).format(
                date=question.question_date, evidence=bundle.rendered
            )
            with ChatClient(settings.eval.gen, cache_dir=None) as generator:
                answer = generator.complete(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": question.question},
                    ],
                    max_tokens=ANSWER_BUDGET,
                )
            with ChatClient(settings.eval.judge, cache_dir=None) as judge:
                grade = _grade(judge, question, _answer(answer))
            loaded = [
                bound.get_memory(UUID(item.ref_id), item.version)
                for item in bundle.sections[0].items
            ]
            sources = {source for item in loaded for source in item.source_event_ids}
            fact_sources = {
                source for item in loaded if item.type == "fact" for source in item.source_event_ids
            }
            results.append(
                {
                    "arm": arm,
                    "correct": grade["correct"],
                    "gold_total": len(gold),
                    "gold_from_loaded": len(sources & gold),
                    "gold_from_facts": len(fact_sources & gold),
                    "facts_loaded": sum(item.type == "fact" for item in loaded),
                    "written_turns": len(turns),
                    "write_calls": len(usage),
                    "write_jobs": len(job_statuses),
                    "write_failed_calls": sum(row.status == "failed" for row in usage),
                    "write_input_tokens": sum(row.input_tokens for row in usage),
                    "write_output_tokens": sum(row.output_tokens for row in usage),
                    "write_ms": build_ms,
                    "memory_ms": memory_ms,
                    "answer_ms": answer.latency_ms,
                    "answer_input_tokens": answer.input_tokens,
                    "answer_output_tokens": answer.output_tokens,
                    "judge_ms": grade["llm_call"]["latency_ms"],
                    "judge_input_tokens": grade["llm_call"]["input_tokens"],
                    "judge_output_tokens": grade["llm_call"]["output_tokens"],
                }
            )
    return {
        "question_id": question.question_id,
        "question_type": question.question_type,
        "arms": results,
    }


def report(rows: list[dict]) -> dict:
    result = {"questions": len(rows), "arms": {}}
    for arm in ARMS:
        records = [(row, item) for row in rows for item in row["arms"] if item["arm"] == arm]
        items = [item for _, item in records]
        summary = {
            "correct": sum(item["correct"] for item in items),
            "question_types": {
                category: {
                    "correct": sum(
                        item["correct"] for row, item in records if row["question_type"] == category
                    ),
                    "scored": sum(row["question_type"] == category for row, _ in records),
                }
                for category in sorted({row["question_type"] for row, _ in records})
            },
            "loaded_gold_question_count": sum(item["gold_from_loaded"] > 0 for item in items),
            "loaded_fact_gold_question_count": sum(item["gold_from_facts"] > 0 for item in items),
        }
        for key in (
            "write_calls",
            "write_jobs",
            "write_failed_calls",
            "write_input_tokens",
            "write_output_tokens",
            "answer_input_tokens",
            "answer_output_tokens",
            "judge_input_tokens",
            "judge_output_tokens",
            "written_turns",
        ):
            summary[key] = sum(item[key] for item in items)
        for key in ("write_ms", "memory_ms", "answer_ms", "judge_ms"):
            summary[key] = _latency(item[key] for item in items)
        if arm != "G3":
            summary["paired"] = {
                "gained": sum(
                    item["correct"] and not row["arms"][0]["correct"] for row, item in records
                ),
                "lost": sum(
                    not item["correct"] and row["arms"][0]["correct"] for row, item in records
                ),
            }
        result["arms"][arm] = summary
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--suite", type=Path, default=SUITE)
    args = parser.parse_args()
    settings = Settings()
    required = (
        settings.task_model("extract"),
        settings.task_model("reconcile"),
        settings.eval.gen,
        settings.eval.judge,
    )
    if settings.embedding.provider != "openai_compatible" or not all(
        item.model for item in required
    ):
        parser.error("Configure a semantic embedding endpoint and the four model tasks")
    path = DATA_DIRECTORY / DATA_FILES["oracle"]
    suite = load_suite(args.suite, "oracle", path)
    questions = _load_questions(path, suite)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    rows_path = args.output / "diagnostics.jsonl"
    manifest = {
        "protocol": "t16-p2-oracle-v1",
        **suite.model_dump(mode="json"),
        "arms": list(ARMS),
        "embedding": settings.embedding.model,
        "fact_extract_model": required[0].model,
        "fact_reconcile_model": required[1].model,
        "answer_model": required[2].model,
        "judge_model": required[3].model,
        "prompt_versions": [CLASSIC_VERSION, ADDITIVE_VERSION, RECONCILE_VERSION],
        "memory_budget": MEMORY_BUDGET,
        "answer_budget": ANSWER_BUDGET,
        "top_k": TOP_K,
    }
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        parser.error("Output already contains another experiment")
    _write(manifest_path, manifest)
    rows = _read_rows(rows_path)
    done = {row["question_id"] for row in rows}
    remaining = [question for question in questions if question.question_id not in done]
    try:
        with ThreadPoolExecutor(max_workers=settings.eval.concurrency) as executor:
            for row in executor.map(lambda question: evaluate(settings, question), remaining):
                _append(rows_path, row)
                rows.append(row)
                print(f"Completed {len(rows)}/{len(questions)} {row['question_id']}", flush=True)
    finally:
        _write(args.output / "metrics.json", report(rows))


if __name__ == "__main__":
    main()
