"""Replay two import tasks and three pinned AMA-Bench questions against both lesson strategies."""

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from run_m2_paired import ANSWER_BUDGET, MEMORY_BUDGET, _isolated_service, _latency, _write
from run_t16_tiers import question
from sqlalchemy import select

from memfxxk.config import Settings, StrategiesSettings
from memfxxk.db import jobs, llm_calls
from memfxxk.domain import Actor
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.lessons import FinishTask, ReviewLesson
from memfxxk.eval.longmemeval.judging import _grade
from memfxxk.eval.longmemeval.qa import ANSWER_PROMPT, _answer, read_prompt
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatClient
from memfxxk.prompts import lesson_distill, trajectory_judge, workflow_induce
from memfxxk.scope import visible_filter
from memfxxk.service import MemoryService

AMA_REV = "a5777378066f53229a94557a7b192435cd027909"
AMA_SHA = "45c36052e1520d87ad9de4114f71c9df42d4aac9cf158c0c353e800b653d65ff"
AMA_QUESTIONS = (
    "78edb435-fcdf-4337-bdae-42254be3ed62",
    "5eff1c78-5f14-4a52-b2df-dffd387b34f9",
    "08afcf17-ca16-475e-a072-983cd95c7f53",
)
ARMS = ("hybrid", "reasoningbank", "awm")


@dataclass(frozen=True)
class Import:
    source: str
    delimiter: str

    @property
    def task(self) -> str:
        return f"Import three structured readings from the {self.source} CSV export."

    def file(self, new: bool = False) -> str:
        rows = [("sensor", "reading"), ("sensor-a", "25"), ("sensor-b", "31"), ("sensor-c", "39")]
        if new:
            rows[1:] = [("sensor-d", "14"), ("sensor-e", "19"), ("sensor-f", "27")]
        return "\n".join(self.delimiter.join(row) for row in rows)


IMPORTS = (Import("east warehouse controller", ";"), Import("north depot gateway", "|"))


def ingest_result(text: str, delimiter: str) -> tuple[bool, str]:
    if delimiter not in {",", ";", "|", "\t"}:
        return False, "Unsupported delimiter."
    rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    if rows[0] != ["sensor", "reading"] or len(rows) != 4:
        return False, "Header did not contain two expected columns. No rows written."
    return True, f"Verified {len(rows) - 1} readings using delimiter {delimiter!r}."


def import_trajectory(case: Import) -> list[IngestEvent]:
    events = [
        IngestEvent(
            kind="message", role="user", session_id="task", turn_id="import", content=case.task
        )
    ]
    for delim in (",", case.delimiter):
        success, text = ingest_result(case.file(), delim)
        events.extend(
            [
                IngestEvent(
                    kind="tool_call",
                    role="assistant",
                    session_id="task",
                    turn_id="import",
                    payload={"name": "load_csv", "delimiter": delim},
                ),
                IngestEvent(
                    kind="tool_result",
                    role="tool",
                    session_id="task",
                    turn_id="import",
                    content=text,
                    payload={"name": "load_csv", "outcome": "success" if success else "failure"},
                ),
            ]
        )
    events.append(
        IngestEvent(
            kind="message",
            role="assistant",
            session_id="task",
            turn_id="import",
            content="The corrected import completed and all three readings were verified.",
        )
    )
    return events


def official_ama(path: Path) -> dict:
    content = path.read_bytes()
    if sha256(content).hexdigest() != AMA_SHA:
        raise ValueError(f"Expected the pinned AMA-Bench revision {AMA_REV}")
    return next(
        item for line in content.splitlines() if (item := json.loads(line))["episode_id"] == 4
    )


def ama_trajectory(episode: dict) -> list[IngestEvent]:
    base = datetime(2025, 7, 1, tzinfo=UTC)
    return [
        IngestEvent(
            kind="message",
            role=role,
            session_id="task",
            turn_id=str(step["turn_idx"]),
            content=text,
            event_time=base + timedelta(seconds=step["turn_idx"]),
        )
        for step in episode["trajectory"]
        for role, text in (
            ("user", f"Step {step['turn_idx']}: take action {step['action']}."),
            ("assistant", f"Step {step['turn_idx']} observation:\n{step['observation']}"),
        )
    ]


def replay(settings: Settings, arm: str, name: str, task: str, events: list, evaluate) -> dict:
    with _isolated_service(settings) as database:
        config = database.settings.model_copy(
            update={
                "strategies": StrategiesSettings(lessons="off" if arm == "hybrid" else arm),
                "llm": settings.llm,
            }
        )
        service = MemoryService(database.engine, config)
        scope = eval_context(f"p8:{name}:{arm}")
        bound = service.for_request(EVAL_PRINCIPAL, scope)
        reviewer = service.for_request(
            EVAL_PRINCIPAL, scope.model_copy(update={"actor": Actor(type="user")})
        )
        started = perf_counter()
        bound.ingest(events)
        marker = bound.finish_task("task", FinishTask(task=task))
        drain(service, kinds={"embed_record", "distill_task"}, max_rounds=100)
        proposals = reviewer.list_lesson_proposals()
        for item in proposals:
            reviewer.review_lesson(
                item.memory_id, ReviewLesson(base_version=item.version, accept=True)
            )
        drain(service, kinds={"embed_record"}, max_rounds=100)
        write_ms = (perf_counter() - started) * 1000
        with service.engine.connect() as conn:
            usage = conn.execute(select(llm_calls).where(visible_filter(llm_calls, scope))).all()
            states = conn.scalars(select(jobs.c.status).where(jobs.c.id.in_(marker.jobs))).all()
        if any(status != "done" for status in states):
            raise RuntimeError(f"{name}/{arm}: task job did not finish")
        return {
            "write_ms": write_ms,
            "write_input": sum(row.input_tokens for row in usage),
            "write_output": sum(row.output_tokens for row in usage),
            "write_calls": len(usage),
            "proposal_count": len(proposals),
            "rows": evaluate(bound),
        }


def _evidence(bound, text: str):
    return bound.build_context(
        BuildContextRequest(
            query=text,
            max_tokens=MEMORY_BUDGET,
            sections=["evidence"],
        )
    )


def import_retest(settings: Settings, bound, case: Import) -> list[dict]:
    query = f"Run the next CSV import from the {case.source} and verify the three new rows."
    start = perf_counter()
    bundle = _evidence(bound, query)
    with ChatClient(settings.eval.gen, cache_dir=None) as gen:
        completion = gen.complete(
            [
                {
                    "role": "system",
                    "content": "Choose one tool argument. Reply with JSON "
                    '{"delimiter":"..."} using a literal comma, semicolon, pipe or tab. '
                    "Use memory evidence when applicable.",
                },
                {"role": "user", "content": query + "\n\n" + bundle.rendered},
            ],
            max_tokens=80,
            response_format={"type": "json_object"},
        )
    try:
        delimiter = json.loads(_answer(completion)).get("delimiter", "")
    except (ValueError, AttributeError):
        delimiter = ""
    success, result = ingest_result(
        case.file(new=True), delimiter if isinstance(delimiter, str) else ""
    )
    return [
        {
            "question": case.source,
            "correct": success,
            "error_recurred": delimiter == ",",
            "result": result,
            "loaded_lessons": sum(
                candidate.record_type == "lesson"
                for section in bundle.sections
                for candidate in section.items
            ),
            "latency_ms": (perf_counter() - start) * 1000,
            "tokens": completion.input_tokens + completion.output_tokens,
        }
    ]


def ama_questions(settings: Settings, bound, episode: dict) -> list[dict]:
    result = []
    selected = {item["question_uuid"]: item for item in episode["qa_pairs"]}
    for key in AMA_QUESTIONS:
        entry = selected[key]
        q = question(key, "multi-session", entry["question"], entry["answer"], "2025/07/02 00:00")
        start = perf_counter()
        bundle = _evidence(bound, q.question)
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
        latency = (perf_counter() - start) * 1000
        answer = _answer(completion)
        with ChatClient(settings.eval.judge, cache_dir=None) as judge:
            grade = _grade(judge, q, answer)
        result.append(
            {
                "question": key,
                "correct": grade["correct"],
                "answer": answer,
                "loaded_lessons": sum(
                    candidate.record_type == "lesson"
                    for section in bundle.sections
                    for candidate in section.items
                ),
                "latency_ms": latency,
                "tokens": completion.input_tokens
                + completion.output_tokens
                + grade["llm_call"]["input_tokens"]
                + grade["llm_call"]["output_tokens"],
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ama", type=Path, default=Path("eval/data/ama-open-end.jsonl"))
    command = parser.parse_args()
    settings = Settings()
    episode = official_ama(command.ama)
    command.output.mkdir(parents=True, exist_ok=True)
    results = {}
    for arm in ARMS:
        data = [
            replay(
                settings,
                arm,
                case.source,
                case.task,
                import_trajectory(case),
                lambda bound: import_retest(settings, bound, case),
            )
            for case in IMPORTS
        ]
        data.append(
            replay(
                settings,
                arm,
                "ama-episode-4",
                episode["task"],
                ama_trajectory(episode),
                lambda bound: ama_questions(settings, bound, episode),
            )
        )
        results[arm] = data
        _write(command.output / "diagnostics.json", results)
        print(f"{arm}: finished 2 repeat imports and 3 AMA questions", flush=True)
    _write(
        command.output / "metrics.json",
        {
            arm: {
                "repeat_success": sum(row["correct"] for item in items[:2] for row in item["rows"]),
                "original_error_recurred": sum(
                    row["error_recurred"] for item in items[:2] for row in item["rows"]
                ),
                "ama_correct": sum(row["correct"] for row in items[2]["rows"]),
                "lessons_loaded": sum(
                    row["loaded_lessons"] for item in items for row in item["rows"]
                ),
                "write_tokens": sum(item["write_input"] + item["write_output"] for item in items),
                "query_tokens": sum(row["tokens"] for item in items for row in item["rows"]),
                "query_ms": _latency(row["latency_ms"] for item in items for row in item["rows"]),
                "proposals": sum(item["proposal_count"] for item in items),
            }
            for arm, items in results.items()
        },
    )
    _write(
        command.output / "manifest.json",
        {
            "ama_revision": AMA_REV,
            "ama_sha256": AMA_SHA,
            "ama_episode": 4,
            "ama_questions": AMA_QUESTIONS,
            "model": settings.eval.gen.model,
            "judge": settings.eval.judge.model,
            "embedding": settings.embedding.model,
            "memory_tokens": MEMORY_BUDGET,
            "prompts": [trajectory_judge.VERSION, lesson_distill.VERSION, workflow_induce.VERSION],
            "memory_models": {
                key: settings.task_model(key).model
                for key in ("trajectory_judge", "lesson_distill", "workflow_induce")
            },
        },
    )


if __name__ == "__main__":
    main()
