"""One replay protocol for T16 strategies and presets, with resumable case results."""

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import UUID

import httpx
from run_m2_paired import (
    ANSWER_BUDGET,
    MEMORY_BUDGET,
    _isolated_service,
    _latency,
    _load_questions,
    _write,
)
from run_t16_lessons import IMPORTS, import_retest, import_trajectory
from run_t16_tiers import LOCOMO, Case, Quiz, locomo, long_dialogue
from sqlalchemy import select

from memfxxk.config import Settings
from memfxxk.db import jobs, llm_calls, transaction
from memfxxk.domain import Actor, Scope
from memfxxk.domain.blocks import CreateBlock
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction, SearchFilters
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.lessons import FinishTask, ReviewLesson
from memfxxk.domain.reflect import ReflectRequest
from memfxxk.eval.longmemeval.dataset import DATA_DIRECTORY, DATA_FILES, file_sha256, iter_turns
from memfxxk.eval.longmemeval.judging import JUDGE_PROMPT, _grade
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.eval.longmemeval.suite import QuestionSuite
from memfxxk.ids import derived_id
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.m2.storage import derived_record
from memfxxk.presets import PRESETS
from memfxxk.prompts import (
    compact,
    edge_extract,
    edge_resolve,
    entity_extract,
    entity_resolve,
    extract,
    lesson_distill,
    memoryos,
    node_summary,
    note_enrich,
    note_evolve,
    note_link,
    opinion_update,
    reconcile,
    reflect,
    rerank,
    sleeptime,
    trajectory_judge,
    workflow_induce,
)
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

SUITE = Path("eval/suites/t16-matrix.json")
ARMS = (
    "baseline",
    "compaction",
    "mem0_classic",
    "mem0",
    "graphiti",
    "amem",
    "letta",
    "memoryos",
    "hindsight",
    "reasoningbank",
    "awm",
    "full",
)
SINGLE = {
    "mem0_classic": {"facts": "mem0_classic"},
    "amem": {"notes": "amem"},
    "memoryos": {"tiers": "memoryos", "blocks": "letta"},
    "hindsight": {"recall": "hindsight", "reflect": "hindsight", "graph": "links"},
    "reasoningbank": {"lessons": "reasoningbank"},
    "awm": {"lessons": "awm"},
}
LABELS = {
    "user_profile": "Long-lived user background and preferences, grounded in conversation.",
    "user_knowledge": "Durable facts, dates, and commitments about the users.",
    "agent_traits": "Durable behavior preferences for the assistant.",
}
PROMPT_VERSIONS = {
    "answer": ANSWER_PROMPT,
    "judge": JUDGE_PROMPT,
    "compact": compact.VERSION,
    "extract_classic": extract.CLASSIC_VERSION,
    "extract_additive": extract.ADDITIVE_VERSION,
    "reconcile": reconcile.VERSION,
    "entity_extract": entity_extract.VERSION,
    "entity_resolve": entity_resolve.VERSION,
    "edge_extract": edge_extract.VERSION,
    "edge_resolve": edge_resolve.VERSION,
    "node_summary": node_summary.VERSION,
    "note_enrich": note_enrich.VERSION,
    "note_link": note_link.VERSION,
    "note_evolve": note_evolve.VERSION,
    "sleeptime": sleeptime.VERSION,
    "persona_update": memoryos.VERSION,
    "reflect": reflect.VERSION,
    "opinion_update": opinion_update.VERSION,
    "rerank": rerank.VERSION,
    "trajectory_judge": trajectory_judge.VERSION,
    "lesson_distill": lesson_distill.VERSION,
    "workflow_induce": workflow_induce.VERSION,
}


def _case_fingerprint(case: Case) -> str:
    return sha256(
        json.dumps(
            {
                "events": [(key, event.model_dump(mode="json")) for key, event in case.events],
                "questions": [q.question.question_id for q in case.quiz],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _longmemeval(suite: QuestionSuite, focused: bool) -> list[Case]:
    dataset = DATA_DIRECTORY / DATA_FILES[suite.dataset]
    if file_sha256(dataset) != suite.dataset_sha256:
        raise ValueError("LongMemEval checksum differs from the frozen suite")
    output = []
    for question in _load_questions(dataset, suite):
        turns = [turn for turn in iter_turns(question) if turn.event_time <= question.asked_at]
        if focused:
            keep = {turn.turn_id for turn in turns if turn.has_answer}
            keep.update(turn.turn_id for turn in [t for t in turns if not t.has_answer][:1])
            turns = [turn for turn in turns if turn.turn_id in keep]
        events = [
            (
                f"{turn.turn_id}:{number}",
                IngestEvent(
                    kind="message",
                    role=message.role,
                    content=message.content,
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    event_time=turn.event_time,
                ),
            )
            for turn in turns
            for number, message in enumerate(turn.messages)
        ]
        gold = [
            f"{turn.turn_id}:{number}"
            for turn in turns
            for number, message in enumerate(turn.messages)
            if message.has_answer
        ]
        output.append(
            Case(
                f"longmemeval-{question.question_id}",
                f"LongMemEval_{suite.dataset}",
                suite.dataset_sha256,
                events,
                [Quiz(question, question.question_type, gold)],
            )
        )
    return output


def cases(path: Path, locomo_file: Path, focused: bool) -> list[Case]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    selected = _longmemeval(QuestionSuite.model_validate(spec["longmemeval"]), focused)
    conversation = locomo(locomo_file)
    conversation = replace(
        conversation,
        quiz=[
            item for item in conversation.quiz if item.question.question_id in spec["locomo_ids"]
        ],
    )
    synthetic = long_dialogue()
    synthetic = replace(
        synthetic,
        quiz=[item for item in synthetic.quiz if item.question.question_id in spec["dialogue_ids"]],
    )
    if focused:
        keys = {key for quiz in conversation.quiz for key in quiz.source_keys}
        positions = {event.turn_id for key, event in conversation.events if key in keys}
        positions.update(event.turn_id for _, event in conversation.events[:12])
        conversation = replace(
            conversation,
            events=[pair for pair in conversation.events if pair[1].turn_id in positions],
        )
        synthetic = replace(
            synthetic,
            events=[
                pair
                for pair in synthetic.events
                if int(pair[1].turn_id) in spec["dialogue_focus_turns"]
            ],
        )
    first = IMPORTS[0]
    task_events = [
        (str(i), event.model_copy(update={"event_time": datetime(2025, 6, 1, tzinfo=UTC)}))
        for i, event in enumerate(import_trajectory(first))
    ]
    return [
        *selected,
        conversation,
        synthetic,
        Case(
            "repeat-import",
            "T09 local import fixture",
            "",
            task_events,
            [],
        ),
    ]


def _settings(settings: Settings, arm: str) -> Settings:
    gen = settings.eval.gen
    reflect = settings.task_model("reflect").model_copy(
        update={
            "model": gen.model,
            "base_url": gen.base_url,
            "api_key": gen.api_key,
        }
    )
    llm = settings.llm.model_copy(update={"tasks": {**settings.llm.tasks, "reflect": reflect}})
    return Settings(
        _env_file=None,
        preset=arm if arm in PRESETS else "baseline",
        llm=llm,
        strategies=SINGLE.get(arm, {}),
        search={"reranker": "llm"} if arm == "hindsight" else {},
        embedding=settings.embedding,
        token_counter=settings.token_counter,
        test_database_url=settings.test_database_url,
        eval=settings.eval,
    )


def _build(service, bound, case: Case, arm: str) -> tuple[dict, dict]:
    if service.settings.strategies.blocks == "letta":
        for label, description in LABELS.items():
            bound.create_block(CreateBlock(label=label, description=description, owner="agent"))
    replayed = list(
        zip(case.events, bound.ingest([event for _, event in case.events]), strict=True)
    )
    originals = {key: row.event_id for (key, _), row in replayed}
    staged: dict[tuple[str, str], list] = defaultdict(list)
    for (_, event), row in replayed:
        if event.kind == "message":
            staged[event.session_id, event.turn_id].append((event, row.event_id))
    if case.name != "repeat-import":
        with transaction(service.engine) as conn:
            for (session, turn), messages in list(staged.items())[:2]:
                derived_record(
                    conn,
                    bound.ctx,
                    Scope.model_validate(bound.ctx.model_dump()),
                    memory_id=derived_id("matrix-note", bound.ctx.project_id, session, turn),
                    version=1,
                    kind="note",
                    content="\n".join(f"[{event.role}] {event.content}" for event, _ in messages),
                    sources=[source for _, source in messages],
                    event_time=messages[-1][0].event_time,
                    session_id=session,
                )
    failures = []
    drain(service, max_rounds=750)
    closed = next(
        (
            (event, row)
            for (_, event), row in reversed(replayed)
            if event.kind == "message" and event.role == "assistant"
        ),
        None,
    )
    if arm in ("compaction", "letta", "full") and closed:
        event, row = closed
        session = event.session_id
        current = bound.get_session(session)
        try:
            draft = bound.prepare_compaction(
                PrepareCompaction(
                    session_id=session,
                    through_seq=row.session_seq,
                    revision=current.revision,
                    target_tokens=350,
                )
            )
            bound.commit_compaction(draft.draft_id)
        except ServiceError as exc:
            failures.append(exc.code)
    if case.name == "repeat-import":
        bound.finish_task("task", FinishTask(task=IMPORTS[0].task))
    drain(service, max_rounds=750)
    if case.name == "repeat-import":
        human = service.for_request(
            EVAL_PRINCIPAL, bound.ctx.model_copy(update={"actor": Actor(type="user")})
        )
        for item in human.list_lesson_proposals():
            human.review_lesson(
                item.memory_id, ReviewLesson(base_version=item.version, accept=True)
            )
        drain(service, max_rounds=750)
    with service.engine.connect() as conn:
        usage = conn.execute(select(llm_calls).where(visible_filter(llm_calls, bound.ctx))).all()
        states = Counter(conn.scalars(select(jobs.c.status)))
    turns = len({(event.session_id, event.turn_id) for _, event in case.events})
    return originals, {
        "calls": len(usage),
        "input": sum(row.input_tokens for row in usage),
        "output": sum(row.output_tokens for row in usage),
        "turns": turns,
        "failed_calls": sum(row.status not in ("succeeded",) for row in usage),
        "jobs": dict(states),
        "errors": failures,
    }


def _answer_case(settings, bound, quiz: Quiz, original: dict) -> dict:
    q = quiz.question
    started = perf_counter()
    if bound.settings.strategies.reflect == "hindsight":
        reply = bound.reflect(
            ReflectRequest(
                query=q.question,
                query_time=q.asked_at,
                max_tokens=MEMORY_BUDGET,
                max_rounds=1,
            )
        )
        answer = reply.answer
        ids, cited = reply.bundle_ids, [str(item.id) for item in reply.citations]
        usage = 0
    else:
        bundle = bound.build_context(
            BuildContextRequest(
                query=q.question,
                sections=["documents", "evidence"],
                max_tokens=MEMORY_BUDGET,
                search_filters=SearchFilters(query_time=q.asked_at, time_to=q.asked_at),
            )
        )
        ids, cited = [bundle.bundle_id], []
        with ChatClient(settings.eval.gen, cache_dir=None) as model:
            answer_result = model.complete(
                [
                    {
                        "role": "system",
                        "content": read_prompt(ANSWER_PROMPT).format(
                            date=q.question_date,
                            evidence=bundle.rendered,
                        ),
                    },
                    {"role": "user", "content": q.question},
                ],
                max_tokens=ANSWER_BUDGET,
            )
        answer = _answer(answer_result)
        usage = answer_result.input_tokens + answer_result.output_tokens
    took = (perf_counter() - started) * 1000
    with ChatClient(settings.eval.judge, cache_dir=None) as model:
        grade = _grade(model, q, answer)
    gold = {original[key] for key in quiz.source_keys}
    selected, dropped = [], []
    seen, packed = set(), set()
    for key in ids:
        for section in bound.get_bundle(key).sections:
            for refs, output in ((section.items, selected), (section.dropped, dropped)):
                for reference in refs:
                    output.append(reference.model_dump(mode="json"))
                    if reference.kind == "memory":
                        sources = bound.get_memory(
                            UUID(reference.ref_id), reference.version
                        ).source_event_ids
                        seen.update(sources)
                        if refs is section.items and reference.decision != "truncated":
                            packed.update(sources)
    return {
        "id": q.question_id,
        "type": quiz.kind,
        "answer": answer,
        "correct": grade["correct"],
        "latency_ms": took,
        "cited_ids": cited,
        "gold": len(gold),
        "seen_gold": len(gold & seen),
        "packed_gold": len(gold & packed),
        "evidence_loss": (
            "no_gold"
            if not gold
            else "search"
            if not gold & seen
            else "packing"
            if not gold & packed
            else "answer"
            if not grade["correct"]
            else "none"
        ),
        "tokens": usage + grade["llm_call"]["input_tokens"] + grade["llm_call"]["output_tokens"],
        "selected": selected,
        "dropped": dropped,
    }


def evaluate(settings: Settings, case: Case, arm: str) -> dict:
    configuration = _settings(settings, arm)
    with _isolated_service(configuration) as isolated:
        service = MemoryService(isolated.engine, configuration)
        scope = eval_context(f"p9:{case.name}:{arm}").model_copy(
            update={"agent_id": "matrix-agent"}
        )
        bound = service.for_request(EVAL_PRINCIPAL, scope)
        started = perf_counter()
        originals, write = _build(service, bound, case, arm)
        write["wall_ms"] = (perf_counter() - started) * 1000
        results = []
        for quiz in case.quiz:
            try:
                results.append(_answer_case(settings, bound, quiz, originals))
            except ServiceError as error:
                results.append(
                    {"id": quiz.question.question_id, "type": quiz.kind, "error": error.code}
                )
            except (httpx.HTTPError, ValueError) as error:
                results.append(
                    {
                        "id": quiz.question.question_id,
                        "type": quiz.kind,
                        "error": type(error).__name__,
                    }
                )
        if case.name == "repeat-import":
            item = import_retest(settings, bound, IMPORTS[0])[0]
            results.append({**item, "id": case.name, "type": "repeat-task"})
        with service.engine.connect() as conn:
            calls = conn.execute(select(llm_calls).where(visible_filter(llm_calls, scope))).all()
        memory_query_tokens = (
            sum(row.input_tokens + row.output_tokens for row in calls)
            - write["input"]
            - write["output"]
        )
        return {
            "case": case.name,
            "arm": arm,
            "write": write,
            "results": results,
            "memory_query_tokens": memory_query_tokens,
        }


def report(arms: list[str], records: list[dict]) -> dict:
    result = {}
    for arm in arms:
        partitions = [record for record in records if record["arm"] == arm]
        rows = [row for record in partitions for row in record["results"]]
        ok = [row for row in rows if "error" not in row]
        writes = [record["write"] for record in partitions]
        write_tokens = sum(row["input"] + row["output"] for row in writes)
        query_tokens = sum(row["tokens"] for row in ok) + sum(
            record["memory_query_tokens"] for record in partitions
        )
        turns = sum(row["turns"] for row in writes)
        result[arm] = {
            "correct": sum(row["correct"] for row in ok),
            "questions": len(rows),
            "per_type": {
                kind: [
                    sum(row["correct"] for row in ok if row["type"] == kind),
                    sum(row["type"] == kind for row in rows),
                ]
                for kind in sorted({row["type"] for row in rows})
            },
            "gold": sum(row.get("gold", 0) for row in ok),
            "seen_gold": sum(row.get("seen_gold", 0) for row in ok),
            "packed_gold": sum(row.get("packed_gold", 0) for row in ok),
            "write_tokens": write_tokens,
            "write_tokens_per_1000_turns": round(write_tokens * 1000 / turns, 2),
            "query_tokens": query_tokens,
            "query_tokens_per_request": round(query_tokens / len(rows), 2),
            "write_ms": _latency(row["wall_ms"] for row in writes),
            "query_ms": _latency(row["latency_ms"] for row in ok) if ok else None,
            "query_failure_rate": (len(rows) - len(ok)) / len(rows),
            "write_failed_calls": sum(row["failed_calls"] for row in writes),
            "write_failure_rate": (
                sum(row["failed_calls"] for row in writes) / sum(row["calls"] for row in writes)
                if any(row["calls"] for row in writes)
                else 0
            ),
            "write_errors": [error for row in writes for error in row["errors"]],
            "write_unfinished_jobs": sum(
                sum(amount for key, amount in row["jobs"].items() if key != "done")
                for row in writes
            ),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--locomo", type=Path, default=LOCOMO)
    parser.add_argument("--histories", choices=("focused", "complete"), default="focused")
    parser.add_argument("--profiles", choices=ARMS, nargs="+", default=list(ARMS))
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    settings = Settings()
    selected = cases(args.suite, args.locomo, args.histories == "focused")
    configurations = {arm: _settings(settings, arm) for arm in args.profiles}
    manifest = {
        "histories": args.histories,
        "profiles": args.profiles,
        "memory_tokens": MEMORY_BUDGET,
        "embedding": settings.embedding.model,
        "answer": settings.eval.gen.model,
        "judge": settings.eval.judge.model,
        "model_default": settings.llm.default.model,
        "prompt_versions": PROMPT_VERSIONS,
        "configurations": {
            arm: config.model_dump(include={"strategies", "sleeptime", "tiers", "search"})
            for arm, config in configurations.items()
        },
        "cases": {
            case.name: {
                "source": case.source,
                "version": case.fingerprint,
                "replay_sha256": _case_fingerprint(case),
                "questions": [q.question.question_id for q in case.quiz],
            }
            for case in selected
        },
    }
    args.output.mkdir(parents=True, exist_ok=True)
    existing = args.output / "manifest.json"
    if existing.exists() and json.loads(existing.read_text(encoding="utf-8")) != manifest:
        raise ValueError("This output directory belongs to a different frozen run")
    _write(existing, manifest)

    def run(unit):
        case, arm = unit
        path = args.output / f"{arm}-{case.name}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        result = evaluate(settings, case, arm)
        _write(path, result)
        print(f"{arm}/{case.name}: {result['write']['calls']} write calls", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as executor:
        records = list(
            executor.map(run, [(case, arm) for case in selected for arm in args.profiles])
        )
    _write(args.output / "metrics.json", report(args.profiles, records))


if __name__ == "__main__":
    main()
