"""Compare independently replayed fact strategies against M1 G3 on frozen Oracle histories."""

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import UUID

from run_m2_paired import (
    ANSWER_BUDGET,
    MEMORY_BUDGET,
    TOP_K,
    _append,
    _edges,
    _isolated_service,
    _latency,
    _load_questions,
    _read_rows,
    _write,
)
from sqlalchemy import select

from memfxxk.config import Settings, StrategiesSettings
from memfxxk.db import jobs, llm_calls, transaction
from memfxxk.domain import Scope
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest
from memfxxk.eval.longmemeval.dataset import DATA_DIRECTORY, DATA_FILES, Question, iter_turns
from memfxxk.eval.longmemeval.judging import _grade
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.eval.longmemeval.suite import load_suite
from memfxxk.ids import derived_id
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.m2.storage import derived_record
from memfxxk.prompts import (
    edge_extract,
    edge_resolve,
    entity_extract,
    entity_resolve,
    node_summary,
    note_enrich,
    note_evolve,
    note_link,
)
from memfxxk.prompts.extract import ADDITIVE_VERSION, CLASSIC_VERSION
from memfxxk.prompts.reconcile import VERSION as RECONCILE_VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

SUITE = Path("eval/suites/longmemeval-oracle-t16-p2.json")
ARMS = ("G3", "mem0_classic", "mem0_additive")
GRAPH_ARMS = ("G3", "links", "graphiti")
GRAPH_SUITE = Path("eval/suites/longmemeval-oracle-t16-p3.json")
GRAPH_BUDGET = 300
GRAPH_TASKS = ("entity_extract", "entity_resolve", "edge_extract", "edge_resolve", "node_summary")
NOTE_ARMS = ("G3", "amem")
NOTE_SUITE = Path("eval/suites/longmemeval-oracle-t16-p4.json")
NOTE_TASKS = ("note_enrich", "note_link", "note_evolve")


def _seed_notes(service, scope, turns, staged, stored, arm) -> None:
    raw = defaultdict(list)
    for (turn, _), result in zip(staged, stored, strict=True):
        raw[turn.turn_id].append(result.event_id)
    with transaction(service.engine) as conn:
        for turn in turns:
            derived_record(
                conn,
                scope,
                Scope.model_validate(scope.model_dump()),
                memory_id=derived_id("oracle-note", arm, turn.turn_id),
                version=1,
                kind="note",
                content="\n".join(
                    f"[{message.role}] {message.content}" for message in turn.messages
                ),
                sources=raw[turn.turn_id],
                event_time=turn.event_time,
            )


def evaluate(settings: Settings, question: Question, arms: tuple[str, ...] = ARMS) -> dict:
    turns = sorted(
        (turn for turn in iter_turns(question) if turn.event_time <= question.asked_at),
        key=lambda turn: turn.event_time,
    )
    staged = [(turn, message) for turn in turns for message in turn.messages]
    results = []
    notes = arms == NOTE_ARMS
    with _isolated_service(settings) as database:
        for arm in arms:
            derived = arm in ("mem0_classic", "mem0_additive")
            graph = arm in ("links", "graphiti", "amem")
            arm_settings = database.settings.model_copy(
                update={
                    "llm": settings.llm,
                    "strategies": StrategiesSettings(
                        facts=arm if derived else "off",
                        mem0_additive_ranking=arm == "mem0_additive",
                        graph=arm if arm in ("links", "graphiti") else "links" if graph else "off",
                        notes="amem" if arm == "amem" else "off",
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
            if notes:
                _seed_notes(service, scope, turns, staged, stored, arm)
            drain(
                service,
                kinds={"embed_record", "extract_facts", "build_entity_graph", "build_amem_note"},
                max_rounds=1000,
            )
            if arm == "links":
                turns_by_memory = {
                    item.memory_ids_touched[0]: turn
                    for (turn, _), item in zip(staged, stored, strict=True)
                }
                for source, target, entity in _edges(turns_by_memory):
                    bound.link(
                        LinkRequest(
                            from_memory_id=source,
                            to_memory_id=target,
                            purpose=f"shared entity: {entity}",
                        )
                    )
            build_ms = (perf_counter() - start_build) * 1000
            with service.engine.connect() as conn:
                usage = conn.execute(
                    select(
                        llm_calls.c.status, llm_calls.c.input_tokens, llm_calls.c.output_tokens
                    ).where(visible_filter(llm_calls, scope))
                ).all()
                job_statuses = conn.scalars(
                    select(jobs.c.status).where(
                        jobs.c.kind.in_(("extract_facts", "build_entity_graph", "build_amem_note")),
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
                    graph_evidence_tokens=GRAPH_BUDGET if arm in GRAPH_ARMS[1:] else 0,
                    search_filters=SearchFilters(
                        time_to=question.asked_at,
                        as_of_valid=question.asked_at,
                        types=["note"] if notes else ["turn", "fact"] if derived else ["turn"],
                        graph_hops=1 if notes and graph else 2 if graph else 0,
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
            graph_refs = bundle.sections[0].items
            results.append(
                {
                    "arm": arm,
                    "correct": grade["correct"],
                    "gold_total": len(gold),
                    "gold_from_loaded": len(sources & gold),
                    "gold_from_facts": len(fact_sources & gold),
                    "facts_loaded": sum(item.type == "fact" for item in loaded),
                    "augmented_notes_loaded": sum(
                        item.note_attributes is not None for item in loaded
                    ),
                    "graph_paths_loaded": sum(
                        item.reason.startswith("graph(") for item in graph_refs
                    ),
                    "graph_edges_loaded": sum(
                        item.graph_edge_id is not None for item in graph_refs
                    ),
                    "graph_gold": any(
                        item.reason.startswith("graph(")
                        and bool(set(memory.source_event_ids) & gold)
                        for item, memory in zip(graph_refs, loaded, strict=True)
                    ),
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


def report(rows: list[dict], arms: tuple[str, ...] = ARMS) -> dict:
    result = {"questions": len(rows), "arms": {}}
    for arm in arms:
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
        if arm in (*GRAPH_ARMS[1:], "amem"):
            summary.update(
                graph_path_questions=sum(item["graph_paths_loaded"] > 0 for item in items),
                graph_edge_questions=sum(item["graph_edges_loaded"] > 0 for item in items),
                graph_gold_questions=sum(item["graph_gold"] for item in items),
            )
        if arm == "amem":
            summary["augmented_note_questions"] = sum(
                item["augmented_notes_loaded"] > 0 for item in items
            )
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


def main(*, mode: Literal["facts", "graph", "notes"] = "facts") -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    default_suite = {"graph": GRAPH_SUITE, "notes": NOTE_SUITE, "facts": SUITE}[mode]
    parser.add_argument("--suite", type=Path, default=default_suite)
    args = parser.parse_args()
    settings = Settings()
    arms = {"graph": GRAPH_ARMS, "notes": NOTE_ARMS, "facts": ARMS}[mode]
    tasks = {"graph": GRAPH_TASKS, "notes": NOTE_TASKS, "facts": ("extract", "reconcile")}[mode]
    model_tasks = {task: settings.task_model(task) for task in tasks}
    required = (*model_tasks.values(), settings.eval.gen, settings.eval.judge)
    if settings.embedding.provider != "openai_compatible" or not all(
        item.model for item in required
    ):
        parser.error(
            "Configure a semantic embedding endpoint, write task models, answerer and judge"
        )
    path = DATA_DIRECTORY / DATA_FILES["oracle"]
    suite = load_suite(args.suite, "oracle", path)
    questions = _load_questions(path, suite)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    rows_path = args.output / "diagnostics.jsonl"
    manifest = {
        "protocol": {
            "graph": "t16-p3-oracle-v1",
            "notes": "t16-p4-oracle-v1",
            "facts": "t16-p2-oracle-v1",
        }[mode],
        **suite.model_dump(mode="json"),
        "arms": list(arms),
        "embedding": settings.embedding.model,
        "answer_model": settings.eval.gen.model,
        "judge_model": settings.eval.judge.model,
        "prompt_versions": [CLASSIC_VERSION, ADDITIVE_VERSION, RECONCILE_VERSION],
        "memory_budget": MEMORY_BUDGET,
        "answer_budget": ANSWER_BUDGET,
        "top_k": TOP_K,
    }
    if mode == "graph":
        manifest.update(
            task_models={task: item.model for task, item in model_tasks.items()},
            graph_evidence_tokens=GRAPH_BUDGET,
            prompt_versions=[
                entity_extract.VERSION,
                entity_resolve.VERSION,
                edge_extract.VERSION,
                edge_resolve.VERSION,
                node_summary.VERSION,
            ],
        )
    elif mode == "notes":
        manifest.update(
            task_models={task: item.model for task, item in model_tasks.items()},
            prompt_versions=[note_enrich.VERSION, note_link.VERSION, note_evolve.VERSION],
            note_seed="verbatim user/assistant turn text with original event IDs for both arms",
        )
    else:
        manifest.update(
            fact_extract_model=model_tasks["extract"].model,
            fact_reconcile_model=model_tasks["reconcile"].model,
        )
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        parser.error("Output already contains another experiment")
    _write(manifest_path, manifest)
    rows = _read_rows(rows_path)
    done = {row["question_id"] for row in rows}
    remaining = [question for question in questions if question.question_id not in done]
    try:
        with ThreadPoolExecutor(max_workers=settings.eval.concurrency) as executor:
            for row in executor.map(lambda question: evaluate(settings, question, arms), remaining):
                _append(rows_path, row)
                rows.append(row)
                print(f"Completed {len(rows)}/{len(questions)} {row['question_id']}", flush=True)
    finally:
        _write(args.output / "metrics.json", report(rows, arms))


if __name__ == "__main__":
    main()
