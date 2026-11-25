import json
from pathlib import Path

import pytest

from memfxxk.eval.longmemeval.dataset import file_sha256, iter_questions, iter_turns
from memfxxk.eval.longmemeval.evidence import RankedEvidence, run_evidence, score_evidence
from memfxxk.eval.longmemeval.ingestion import ingest_dataset, select_questions
from memfxxk.eval.longmemeval.storage import EVAL_PRINCIPAL, eval_context
from memfxxk.eval.longmemeval.suite import QuestionSuite, load_suite
from memfxxk.service import MemoryService

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "longmemeval-mini.json"


@pytest.mark.integration
def test_evidence_suite_runs_on_scoped_postgres(service: MemoryService, tmp_path: Path) -> None:
    multi, abstention = iter_questions(FIXTURE)
    suite = QuestionSuite(
        dataset="s",
        dataset_sha256=file_sha256(FIXTURE),
        question_ids=(abstention.question_id, multi.question_id),
    )
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(suite.model_dump_json(), encoding="utf-8")
    assert load_suite(suite_file, "s", FIXTURE) == suite
    with pytest.raises(ValueError, match="pinned dataset"):
        load_suite(suite_file, "oracle", FIXTURE)
    with pytest.raises(ValueError, match="unknown question IDs"):
        select_questions(FIXTURE, question_ids=("unavailable",))
    ingest_dataset(service, FIXTURE, "s", question_ids=suite.question_ids)
    bound = service.for_request(EVAL_PRINCIPAL, eval_context(multi.question_id))
    assert bound.eval_evidence_coverage(multi.asked_at) == (2, 2)
    other = service.for_request(EVAL_PRINCIPAL, eval_context("another-question"))
    assert other.eval_evidence_coverage(multi.asked_at) == (0, 0)

    settings = service.settings.model_copy(
        update={
            "embedding": service.settings.embedding.model_copy(update={"model": "unindexed-model"})
        }
    )
    unindexed = MemoryService(service.engine, settings)
    with pytest.raises(ValueError, match="Finish indexing"):
        run_evidence(unindexed, FIXTURE, suite, tmp_path, "unindexed", {}, channels=("vector",))

    output = run_evidence(service, FIXTURE, suite, tmp_path, "mini", {}, channels=("lexical",))
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["questions"] == 2 and metrics["abstention"] == 1
    assert metrics["channels"]["memfxxk-lexical"]["overall"]["scores"]["session_recall_all@5"] == 1
    rows = [
        json.loads(row)
        for row in (output / "per_question.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["question_id"] for row in rows[::2]] == list(suite.question_ids)
    assert all(row["scores"] is None for row in rows if row["abstention"])
    future = {str(turn.memory_id) for turn in iter_turns(multi) if turn.event_time > multi.asked_at}
    assert future
    assert all(future.isdisjoint(row["turn_ids"]) for row in rows)
    assert all(row["turn_ids"] for row in rows if row["backend"] == "recent-turns")


def test_evidence_rejects_leaks_and_skips_unmapped_id_scores() -> None:
    question = next(iter_questions(FIXTURE))
    visible = {
        str(turn.memory_id): turn
        for turn in iter_turns(question)
        if turn.event_time <= question.asked_at
    }
    with pytest.raises(ValueError, match="out-of-scope turn"):
        score_evidence(question, visible, RankedEvidence([None, "another-question"], 0))
    result = score_evidence(question, visible, RankedEvidence([None, next(iter(visible))], 0))
    assert result and all(value is None for value in result.values())
