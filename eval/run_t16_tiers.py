"""Compare MemoryOS loading with the existing loader on identical long histories."""

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import UUID

from run_m2_paired import ANSWER_BUDGET, MEMORY_BUDGET, _isolated_service, _latency, _write
from run_t16_blocks import QUIZ, dialogue
from sqlalchemy import select

from memfxxk.config import Settings, StrategiesSettings
from memfxxk.db import jobs, llm_calls
from memfxxk.domain.blocks import CreateBlock
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.eval.longmemeval.dataset import Question
from memfxxk.eval.longmemeval.judging import _grade
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.prompts.memoryos import VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

LOCOMO = Path("eval/data/locomo10.json")
LOCOMO_COMMIT = "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
LOCOMO_SHA = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
LOCOMO_INDEXES = (0, 2, 7, 14, 15, 21, 42, 145)
LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "abstain"}
JUDGE_TYPES = {
    1: "multi-session",
    2: "temporal-reasoning",
    3: "multi-session",
    4: "single-session-user",
    5: "single-session-user",
}


@dataclass(frozen=True)
class Quiz:
    question: Question
    kind: str
    source_keys: list[str]


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    fingerprint: str
    events: list[tuple[str, IngestEvent]]
    quiz: list[Quiz]


def question(key, kind, text, answer, time) -> Question:
    return Question(
        question_id=key,
        question_type=kind,
        question=text,
        answer=answer,
        question_date=time,
        haystack_session_ids=[],
        haystack_dates=[],
        haystack_sessions=[],
        answer_session_ids=[],
    )


def long_dialogue() -> Case:
    turns = dialogue()
    base = datetime(2025, 6, 1, tzinfo=UTC)
    return Case(
        "long-dialogue",
        "T16 64-turn dialogue",
        sha256(json.dumps([turns, QUIZ]).encode()).hexdigest(),
        [
            (
                f"{i}:{role}",
                IngestEvent(
                    kind="message",
                    session_id="dialogue",
                    turn_id=str(i),
                    role=role,
                    content=text,
                    event_time=base + timedelta(hours=i),
                ),
            )
            for i, pair in enumerate(turns, start=1)
            for role, text in zip(("user", "assistant"), pair, strict=True)
        ],
        [
            Quiz(
                question(f"t16-long-{i}", kind, prompt, expected, "2025/06/04 00:00"),
                kind,
                [f"{source}:user"],
            )
            for i, (kind, prompt, expected, source) in enumerate(QUIZ, start=1)
        ],
    )


def locomo(path: Path) -> Case:
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != LOCOMO_SHA:
        raise ValueError(f"Expected LoCoMo data at official commit {LOCOMO_COMMIT}")
    sample = next(row for row in json.loads(raw) if row["sample_id"] == "conv-26")
    chat = sample["conversation"]
    events = []
    last = None
    for session, entries in chat.items():
        if not isinstance(entries, list) or not session.startswith("session_"):
            continue
        date = datetime.strptime(chat[f"{session}_date_time"], "%I:%M %p on %d %B, %Y").replace(
            tzinfo=UTC
        )
        last = max(date, last) if last else date
        for i, item in enumerate(entries):
            events.append(
                (
                    item["dia_id"],
                    IngestEvent(
                        kind="message",
                        session_id=session,
                        turn_id=f"{session}:{i // 2}",
                        role="assistant" if i % 2 or i == len(entries) - 1 else "user",
                        content=f"{item['speaker']}: {item['text']}"
                        + (
                            f" [Image caption: {item['blip_caption']}]"
                            if item.get("blip_caption")
                            else ""
                        ),
                        event_time=date + timedelta(seconds=i),
                    ),
                )
            )
    assert last is not None
    latest = (last + timedelta(days=1)).strftime("%Y/%m/%d %H:%M")
    quiz = []
    for i in LOCOMO_INDEXES:
        item = sample["qa"][i]
        category = item["category"]
        answer = str(item.get("answer", "No information available"))
        if category == 3:
            answer = answer.split(";")[0].strip()
        suffix = "_abs" if category == 5 else ""
        quiz.append(
            Quiz(
                question(
                    f"locomo-26-{i}{suffix}",
                    JUDGE_TYPES[category],
                    item["question"],
                    answer,
                    latest,
                ),
                LOCOMO_CATEGORIES[category],
                [
                    identifier
                    for evidence in item.get("evidence", [])
                    for identifier in re.findall(r"D\d+:\d+", evidence)
                ],
            )
        )
    return Case("locomo-conv-26", f"snap-research/locomo@{LOCOMO_COMMIT}", LOCOMO_SHA, events, quiz)


def evaluate(settings: Settings, case: Case, arm: str) -> tuple[dict, list[dict]]:
    enabled = arm == "memoryos"
    with _isolated_service(settings) as database:
        configuration = database.settings.model_copy(
            update={
                "llm": settings.llm,
                "strategies": StrategiesSettings(
                    blocks="letta" if enabled else "off",
                    tiers="memoryos" if enabled else "off",
                ),
            }
        )
        service = MemoryService(database.engine, configuration)
        scope = eval_context(f"{case.name}:{arm}").model_copy(update={"agent_id": "eval-agent"})
        bound = service.for_request(EVAL_PRINCIPAL, scope)
        if enabled:
            for label, description in (
                ("user_profile", "Stable preferences, experiences, and personal background"),
                ("user_knowledge", "Durable facts, dates, and commitments about the users"),
                ("agent_traits", "Durable behavior preferences for the assistant"),
            ):
                bound.create_block(CreateBlock(label=label, description=description, owner="agent"))
        started = perf_counter()
        stored = bound.ingest([event for _, event in case.events])
        originals = {
            source: item.event_id for (source, _), item in zip(case.events, stored, strict=True)
        }
        turns = len({(event.session_id, event.turn_id) for _, event in case.events})
        drain(service, kinds={"embed_record", "tier_project", "tier_summarize"}, max_rounds=1000)
        write_ms = (perf_counter() - started) * 1000
        with service.engine.connect() as conn:
            calls = conn.execute(select(llm_calls).where(visible_filter(llm_calls, scope))).all()
            statuses = dict(
                Counter(
                    conn.scalars(
                        select(jobs.c.status).where(
                            jobs.c.kind.in_(("tier_project", "tier_summarize")),
                        )
                    )
                )
            )
        if any(status != "done" for status in statuses):
            raise RuntimeError(f"{arm}: incomplete tier jobs: {statuses}")
        print(f"{case.name}/{arm}: wrote {turns} turns, {len(calls)} model calls", flush=True)
        rows = []
        for quiz in case.quiz:
            q = quiz.question
            planned = perf_counter()
            bundle = bound.build_context(
                BuildContextRequest(
                    query=q.question,
                    max_tokens=MEMORY_BUDGET,
                    k=8,
                    sections=["documents", "evidence"],
                )
            )
            assembly_ms = (perf_counter() - planned) * 1000
            with ChatClient(settings.eval.gen, cache_dir=None) as gen:
                completion = gen.complete(
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
            answer = _answer(completion)
            with ChatClient(settings.eval.judge, cache_dir=None) as judge:
                grade = _grade(judge, q, answer)
            selected = [item for section in bundle.sections for item in section.items]
            gold = {originals[key] for key in quiz.source_keys}
            matched = set()
            for item in selected:
                if item.kind == "memory":
                    matched.update(
                        bound.get_memory(UUID(item.ref_id), item.version).source_event_ids
                    )
            rows.append(
                {
                    "id": q.question_id,
                    "kind": quiz.kind,
                    "correct": grade["correct"],
                    "answer": answer,
                    "references": quiz.source_keys,
                    "gold": len(gold),
                    "gold_found": len(gold & matched),
                    "assembly_ms": assembly_ms,
                    "answer_ms": completion.latency_ms,
                    "answer_input": completion.input_tokens,
                    "answer_output": completion.output_tokens,
                    "judge_input": grade["llm_call"]["input_tokens"],
                    "judge_output": grade["llm_call"]["output_tokens"],
                    "memory_tokens": bundle.total_tokens,
                    "selected": [item.model_dump(mode="json") for item in selected],
                }
            )
        summary = {
            "turns": turns,
            "write_ms": write_ms,
            "write_model_calls": len(calls),
            "write_jobs": statuses,
            "write_failed_calls": sum(row.status != "succeeded" for row in calls),
            "write_input": sum(row.input_tokens for row in calls),
            "write_output": sum(row.output_tokens for row in calls),
            "correct": sum(row["correct"] for row in rows),
            "by_type": {
                kind: {
                    "correct": sum(row["correct"] for row in rows if row["kind"] == kind),
                    "total": sum(row["kind"] == kind for row in rows),
                }
                for kind in {row["kind"] for row in rows}
            },
            "gold_questions": sum(row["gold_found"] > 0 for row in rows),
            "gold_total": sum(row["gold"] for row in rows),
            "gold_found": sum(row["gold_found"] for row in rows),
            "assembly_ms": _latency(row["assembly_ms"] for row in rows),
            "answer_ms": _latency(row["answer_ms"] for row in rows),
            "qa_tokens": sum(
                sum(
                    row[key]
                    for key in (
                        "answer_input",
                        "answer_output",
                        "judge_input",
                        "judge_output",
                    )
                )
                for row in rows
            ),
        }
        return summary, rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--locomo", default=LOCOMO, type=Path)
    args = parser.parse_args()
    settings = Settings()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = [long_dialogue(), locomo(args.locomo)]
    _write(
        args.output / "manifest.json",
        {
            "cases": {
                case.name: {
                    "source": case.source,
                    "sha256": case.fingerprint,
                    "questions": [q.question.question_id for q in case.quiz],
                }
                for case in cases
            },
            "embedding": settings.embedding.model,
            "model": settings.eval.gen.model,
            "judge": settings.eval.judge.model,
            "persona_model": settings.task_model("persona_update").model,
            "budget": MEMORY_BUDGET,
            "segment_prompt": VERSION,
        },
    )
    results = {}
    for case in cases:
        for arm in ("hybrid", "memoryos"):
            summary, rows = evaluate(settings, case, arm)
            _write(args.output / f"{case.name}-{arm}.json", rows)
            results[f"{case.name}/{arm}"] = summary
            _write(args.output / "metrics.json", results)
