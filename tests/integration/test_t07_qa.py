import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from memfxxk.eval.llm import ChatMessage, ChatResult
from memfxxk.eval.longmemeval.dataset import file_sha256, iter_questions, iter_turns
from memfxxk.eval.longmemeval.evidence import run_evidence
from memfxxk.eval.longmemeval.ingestion import ingest_dataset
from memfxxk.eval.longmemeval.judging import judge_results
from memfxxk.eval.longmemeval.qa import QAOptions, run_qa
from memfxxk.eval.longmemeval.qa_evidence import inject_evidence, load_evidence_run
from memfxxk.eval.longmemeval.suite import QuestionSuite
from memfxxk.service import MemoryService
from memfxxk.text import SimpleCounter

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).parents[1] / "fixtures/longmemeval-mini.json"


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class FakeGenerator:
    model = "fake-generator"

    def __init__(self) -> None:
        current, other = list(iter_questions(FIXTURE))
        self.future_id = next(
            str(turn.memory_id)
            for turn in iter_turns(current)
            if turn.event_time > current.asked_at
        )
        self.other_id = str(next(iter_turns(other)).memory_id)
        self.requests: list[list[ChatMessage]] = []

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ChatMessage] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.requests.append(list(messages))
        if tools is not None:
            names = {tool["function"]["name"] for tool in tools}
            assert names == {"memory_search", "memory_get"}
            prior = sum(message["role"] == "tool" for message in messages)
            if prior == 0:
                name = "memory_search"
                args = {"query": "alpha zulu", "time_to": "2029-01-01T00:00:00Z"}
            else:
                name = "memory_get"
                args = {"id": self.future_id if prior == 1 else self.other_id, "window": 5}
            return ChatResult(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call-{prior}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
                11,
                3,
            )
        answer = (
            "I don't know." if "favorite number" in messages[1]["content"] else "Alpha and zulu."
        )
        return ChatResult({"role": "assistant", "content": answer}, 15, 3)


class FakeJudge:
    model = "fake-judge"

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ChatMessage] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        assert max_tokens == 10
        return ChatResult({"role": "assistant", "content": "Yes"}, 7, 1)


def test_t07_all_groups_and_judgement(service: MemoryService, tmp_path: Path) -> None:
    ingest_dataset(service, FIXTURE, "s")
    out = tmp_path / "run"
    generator = FakeGenerator()
    options = QAOptions(
        groups=("G1", "G2", "G3", "G4"),
        channels="lexical",
        max_tool_calls=3,
        concurrency=1,
    )
    assert run_qa(service, FIXTURE, "s", out, generator, options) == out
    assert judge_results(out, FakeJudge()) == out
    assert {
        "manifest.json",
        "metrics.md",
        "costs.json",
        "per_question.jsonl",
        *(f"hypotheses.{group}.jsonl" for group in options.groups),
        *(f"judgements.{group}.jsonl" for group in options.groups),
    }.issubset(item.name for item in out.iterdir())
    assert lines(out / "hypotheses.G1.jsonl")[0] == {
        "question_id": "mini_multi",
        "hypothesis": "Alpha and zulu.",
    }
    observations = lines(out / "per_question.jsonl")
    g4 = [row for row in observations if row["group"] == "G4"]
    assert len(g4) == 2 and all(row["tool_limit_reached"] for row in g4)
    assert all(row["tool_call_count"] == 3 for row in g4)
    assert g4[0]["answer_session_retrieved"]
    assert g4[0]["retrieval_timings_ms"]
    g3 = next(row for row in observations if row["group"] == "G3")
    assert g3["answer_session_retrieved"]
    assert g3["retrieval_timings_ms"]
    assert "future_session" not in generator.requests[2][0]["content"]
    forced_messages = generator.requests[6]
    replies = [message["content"] for message in forced_messages if message["role"] == "tool"]
    assert "future_session" not in replies[0]
    assert all("not found" in reply.lower() for reply in replies[1:])
    assert "builtin(fake-judge)" in (out / "metrics.md").read_text(encoding="utf-8")


def test_t07_full_history_skips_long_input(service: MemoryService, tmp_path: Path) -> None:
    out = tmp_path / "run"
    generator = FakeGenerator()
    options = QAOptions(groups=("G2",), max_context_tokens=1)
    run_qa(service, FIXTURE, "s", out, generator, options, limit=1)
    assert lines(out / "per_question.jsonl")[0]["status"] == "skipped_too_long"
    assert lines(out / "hypotheses.G2.jsonl") == []
    assert generator.requests == []


def test_qa_replays_frozen_evidence_with_same_format_and_budget(
    service: MemoryService, tmp_path: Path
) -> None:
    current, other = iter_questions(FIXTURE)
    ids = (other.question_id, current.question_id)
    suite = QuestionSuite(dataset="s", dataset_sha256=file_sha256(FIXTURE), question_ids=ids)
    ingest_dataset(service, FIXTURE, "s", question_ids=ids)
    evidence_path = run_evidence(
        service, FIXTURE, suite, tmp_path, "replay", {}, channels=("lexical",)
    )
    groups = ("recent-turns", "memfxxk-lexical")
    evidence = load_evidence_run(evidence_path, suite, groups)
    with pytest.raises(ValueError, match="missing question"):
        load_evidence_run(evidence_path, suite, ("mem0-raw",))
    reversed_suite = suite.model_copy(update={"question_ids": tuple(reversed(ids))})
    with pytest.raises(ValueError, match="match the frozen question suite"):
        load_evidence_run(evidence_path, reversed_suite, groups)

    generator = FakeGenerator()
    options = QAOptions(groups=groups, evidence_budget=36, concurrency=1)
    out = run_qa(
        service,
        FIXTURE,
        "s",
        tmp_path / "qa",
        generator,
        options,
        question_ids=ids,
        evidence_run=evidence,
    )
    observations = lines(out / "per_question.jsonl")
    assert [row["question_id"] for row in observations[::2]] == list(ids)
    assert any(row["answer_session_retrieved"] for row in observations)
    assert all(row["injected_tokens"] <= 36 for row in observations)
    for messages in generator.requests:
        assert "future_session" not in messages[0]["content"]
        assert '<turn date="' in messages[0]["content"]
    with pytest.raises(ValueError, match="out-of-scope turn"):
        inject_evidence(current, (generator.future_id,), k=8, budget=36, counter=SimpleCounter())
