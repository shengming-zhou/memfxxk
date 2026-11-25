import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from memfxxk.db import events
from memfxxk.eval.cli import eval_cli
from memfxxk.eval.longmemeval.dataset import file_sha256
from memfxxk.eval.longmemeval.ingestion import ingest_dataset
from memfxxk.eval.longmemeval.retrieval import RetrievalOptions, run_retrieval
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "longmemeval-mini.json"


def test_fixture_resumes_and_produces_timed_retrieval_artifacts(
    service: MemoryService, engine: Engine, tmp_path: Path
) -> None:
    first = ingest_dataset(service, FIXTURE, "oracle", limit=1)
    assert (first.inserted, first.skipped) == (1, 0)
    second = ingest_dataset(service, FIXTURE, "oracle")
    assert (second.inserted, second.skipped) == (1, 1)
    assert (second.cache_hits, second.cache_misses) == (1, 0)

    with engine.connect() as connection:
        event_count = connection.scalar(select(func.count()).select_from(events))
    third = ingest_dataset(service, FIXTURE, "oracle")
    assert (third.inserted, third.skipped) == (0, 2)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(events)) == event_count

    result_dir = run_retrieval(
        service, FIXTURE, RetrievalOptions(dataset="oracle", k_list=(1, 2)), tmp_path
    )
    assert {path.name for path in result_dir.iterdir()} == {
        "manifest.json",
        "metrics.json",
        "metrics.md",
        "latency.json",
        "per_question.jsonl",
    }
    metrics = json.loads((result_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["questions"] == 2
    assert metrics["abstention"] == 1
    for channel in ("lexical", "vector", "hybrid"):
        assert metrics["channels"][channel]["overall"]["questions"] == 1
        scores = metrics["channels"][channel]["by_type"]["multi-session"]["scores"]
        for level in ("session", "turn"):
            assert scores[f"{level}_recall_any@1"] == 1.0
            assert scores[f"{level}_recall_all@1"] == 0.0
            assert scores[f"{level}_recall_all@2"] == 1.0
        assert scores["session_ndcg@2"] == 1.0

    rows = [
        json.loads(line)
        for line in (result_dir / "per_question.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 4
    for row in rows:
        if row.get("abstention"):
            assert row["question_id"] == "mini_abs"
        else:
            assert set(row["ranked_sessions"]) == {
                "mini_multi:alpha_session",
                "mini_multi:zulu_session",
            }
    manifest = json.loads((result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_sha256"] == file_sha256(FIXTURE)
    assert manifest["record_count"] == 4
    latency = json.loads((result_dir / "latency.json").read_text(encoding="utf-8"))
    assert set(latency["hybrid"]["total"]) == {"p50", "p95", "p99"}
    report = CliRunner().invoke(eval_cli, ["report", str(result_dir), str(result_dir)])
    assert report.exit_code == 0, report.output
    assert "1.0000" in report.output


def test_eval_reset_replaces_the_dataset_split(service: MemoryService, engine: Engine) -> None:
    ingest_dataset(service, FIXTURE, "oracle")
    with pytest.raises(ValueError, match="--reset"):
        ingest_dataset(service, FIXTURE, "s")

    summary = ingest_dataset(service, FIXTURE, "s", reset=True)
    assert (summary.inserted, summary.skipped) == (2, 0)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(events)) == 8
