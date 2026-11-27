"""Replay a frozen long dialogue with and without Letta-style block consolidation."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import UUID

from run_m2_paired import ANSWER_BUDGET, MEMORY_BUDGET, _isolated_service, _latency, _write
from sqlalchemy import select

from memfxxk.config import Settings, SleeptimeSettings, StrategiesSettings
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
from memfxxk.prompts.sleeptime import VERSION
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

TOPICS = (
    "catalogue records",
    "invoice templates",
    "billing dashboard",
    "backups",
    "design handoff",
    "warehouse labels",
    "support queue",
    "chapter outline",
    "tests for imports",
    "deployment notes",
    "data retention",
    "on-call schedule",
    "front desk signage",
    "archive photos",
    "vendor renewals",
    "inventory scans",
)
HISTORY = {
    2: (
        "Please use matte finishes for everything on my desk. Reflections give me headaches.",
        "I'll remember to choose matte finishes for your desk.",
    ),
    7: (
        "For any accessibility reviews, I prefer recordings over live calls. "
        "I like to listen slowly and make notes.",
        "Recorded walkthroughs it is.",
    ),
    18: (
        "I promised to send Mira the signed venue sketch on June 14.",
        "I noted June 14 for sending Mira the signed venue sketch.",
    ),
    24: (
        "Let's ship the printed festival invitations on June 20.",
        "June 20 for dispatching the printed festival invitations.",
    ),
    33: (
        "Mira approved moving our signed venue sketch handoff from June 14 to June 18.",
        "I'll use June 18 as the current due date for Mira's sketch.",
    ),
    46: (
        "The festival producer delayed mailing the invitations to June 27. "
        "June 20 was only the original dispatch date.",
        "June 27 is the replacement shipping date.",
    ),
}
QUIZ = (
    (
        "single-session-preference",
        "What finish would you pick for my new desktop lamp?",
        "matte",
        2,
    ),
    (
        "single-session-preference",
        "How should I join an accessibility walkthrough?",
        "recording",
        7,
    ),
    ("knowledge-update", "On what date is my promised drawing going to Mira?", "June 18", 33),
    (
        "knowledge-update",
        "When are we actually putting the paper festival invites in the post?",
        "June 27",
        46,
    ),
)


def dialogue() -> list[tuple[str, str]]:
    exchanges = (
        (
            "Can you check the materials and owners for {topic}?",
            "I listed available files and grouped the open questions for {topic}.",
        ),
        (
            "I read the draft for {topic}. Where should the checklist go?",
            "Please keep the checklist for {topic} next to its project notes.",
        ),
        (
            "What's the next verification step for {topic}?",
            "Compare the latest status for {topic} with the prior version, then note differences.",
        ),
        (
            "Help document the review choices we considered for {topic}.",
            "I recorded the alternatives for {topic} and left unanswered points for the team.",
        ),
    )
    return [
        HISTORY.get(
            i,
            tuple(
                string.format(topic=TOPICS[(i - 1) % len(TOPICS)])
                for string in exchanges[(i - 1) // len(TOPICS)]
            ),
        )
        for i in range(1, 65)
    ]


def evaluate(settings: Settings, output: Path) -> None:
    turns = dialogue()
    rows = []
    base = datetime(2025, 6, 1, tzinfo=UTC)
    with _isolated_service(settings) as database:
        for arm in ("hybrid", "letta"):
            enabled = arm == "letta"
            configuration = database.settings.model_copy(
                update={
                    "llm": settings.llm,
                    "strategies": StrategiesSettings(blocks="letta" if enabled else "off"),
                    "sleeptime": SleeptimeSettings(every_turns=16, recent_turns=16),
                }
            )
            service = MemoryService(database.engine, configuration)
            scope = eval_context(f"t16-p5:{arm}").model_copy(update={"agent_id": "dialogue-eval"})
            bound = service.for_request(EVAL_PRINCIPAL, scope)
            if enabled:
                for label, description in (
                    (
                        "user_preferences",
                        "Long-lived user preferences used to personalize responses.",
                    ),
                    (
                        "commitments",
                        "Current agreements and corrections, including people and dates.",
                    ),
                ):
                    bound.create_block(
                        CreateBlock(
                            label=label,
                            description=description,
                            value="",
                            owner="agent",
                            limit=1200,
                        )
                    )
            started = perf_counter()
            stored = bound.ingest(
                [
                    IngestEvent(
                        kind="message",
                        session_id=f"dialogue:{arm}",
                        turn_id=str(i),
                        role=role,
                        content=text,
                        event_time=base + timedelta(hours=i),
                    )
                    for i, exchange in enumerate(turns, start=1)
                    for role, text in zip(("user", "assistant"), exchange, strict=True)
                ]
            )
            drain(service, kinds={"embed_record", "sleeptime_consolidate"}, max_rounds=100)
            write_ms = (perf_counter() - started) * 1000
            with service.engine.connect() as conn:
                calls = conn.execute(
                    select(llm_calls).where(visible_filter(llm_calls, scope))
                ).all()
                statuses = conn.scalars(
                    select(jobs.c.status).where(
                        jobs.c.kind == "sleeptime_consolidate",
                        jobs.c.payload["scope"]["project_id"].as_string() == scope.project_id,
                    )
                ).all()
            if enabled and (len(calls) != 4 or any(status != "done" for status in statuses)):
                raise RuntimeError(f"{arm}: background jobs or model calls did not finish")
            print(f"{arm}: {len(calls)} memory-model calls in {write_ms / 1000:.2f}s", flush=True)
            for number, (kind, prompt, expected, gold_turn) in enumerate(QUIZ, start=1):
                gold = stored[(gold_turn - 1) * 2].event_id
                planned = perf_counter()
                bundle = bound.build_context(
                    BuildContextRequest(
                        query=prompt,
                        max_tokens=MEMORY_BUDGET,
                        k=8,
                        sections=["documents", "evidence"],
                    )
                )
                assemble_ms = (perf_counter() - planned) * 1000
                question = Question(
                    question_id=f"t16-p5-{number}",
                    question_type=kind,
                    question=prompt,
                    answer=expected,
                    question_date="2025/06/04 00:00",
                    haystack_session_ids=[],
                    haystack_dates=[],
                    haystack_sessions=[],
                    answer_session_ids=[],
                )
                with ChatClient(settings.eval.gen, cache_dir=None) as generator:
                    completion = generator.complete(
                        [
                            {
                                "role": "system",
                                "content": read_prompt(ANSWER_PROMPT).format(
                                    date=question.question_date,
                                    evidence=bundle.rendered,
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=ANSWER_BUDGET,
                    )
                answer = _answer(completion)
                with ChatClient(settings.eval.judge, cache_dir=None) as judge:
                    grade = _grade(judge, question, answer)
                evidence = [
                    item
                    for section in bundle.sections
                    for item in section.items
                    if item.kind == "memory"
                ]
                rows.append(
                    {
                        "arm": arm,
                        "question": number,
                        "kind": kind,
                        "correct": grade["correct"],
                        "answer": answer,
                        "reference_answer": expected,
                        "gold_raw_loaded": any(
                            gold
                            in bound.get_memory(
                                UUID(item.ref_id),
                                item.version,
                            ).source_event_ids
                            for item in evidence
                        ),
                        "gold_text_in_bundle": expected.lower() in bundle.rendered.lower(),
                        "selected": [
                            item.model_dump(mode="json")
                            for section in bundle.sections
                            for item in section.items
                        ],
                        "memory_tokens": bundle.total_tokens,
                        "query_ms": assemble_ms,
                        "answer_ms": completion.latency_ms,
                        "answer_input": completion.input_tokens,
                        "answer_output": completion.output_tokens,
                        "judge_input": grade["llm_call"]["input_tokens"],
                        "judge_output": grade["llm_call"]["output_tokens"],
                    }
                )
            _write(
                output / f"{arm}-write.json",
                {
                    "turns": len(turns),
                    "write_ms": write_ms,
                    "calls": len(calls),
                    "failed_calls": sum(row.status != "succeeded" for row in calls),
                    "input_tokens": sum(row.input_tokens for row in calls),
                    "output_tokens": sum(row.output_tokens for row in calls),
                    "blocks": [block.model_dump(mode="json") for block in bound.list_blocks()]
                    if enabled
                    else [],
                },
            )
            _write(output / "diagnostics.json", rows)
    _write(
        output / "metrics.json",
        {
            arm: {
                kind: sum(
                    row["correct"] for row in rows if row["arm"] == arm and row["kind"] == kind
                )
                for kind in ("single-session-preference", "knowledge-update")
            }
            | {
                "query_ms": _latency(row["query_ms"] for row in rows if row["arm"] == arm),
                "answer_ms": _latency(row["answer_ms"] for row in rows if row["arm"] == arm),
                "gold_raw_loaded": sum(row["gold_raw_loaded"] for row in rows if row["arm"] == arm),
                "gold_text_in_bundle": sum(
                    row["gold_text_in_bundle"] for row in rows if row["arm"] == arm
                ),
                "answer_tokens": sum(
                    row["answer_input"] + row["answer_output"] for row in rows if row["arm"] == arm
                ),
                "judge_tokens": sum(
                    row["judge_input"] + row["judge_output"] for row in rows if row["arm"] == arm
                ),
            }
            for arm in ("hybrid", "letta")
        },
    )
    _write(
        output / "manifest.json",
        {
            "dataset_sha256": sha256(json.dumps([turns, QUIZ]).encode()).hexdigest(),
            "model": settings.eval.gen.model,
            "judge": settings.eval.judge.model,
            "sleeptime_model": settings.task_model("sleeptime").model,
            "embedding": settings.embedding.model,
            "budget": MEMORY_BUDGET,
            "sleeptime_prompt": VERSION,
            "turns": len(turns),
            "questions": len(QUIZ),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    evaluate(Settings(), args.output)
