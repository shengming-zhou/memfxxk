import json
import re
import subprocess
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Literal

from memfxxk import text as tokenizer
from memfxxk.domain.retrieval import SearchChannel, SearchRequest
from memfxxk.service import MemoryService

from .dataset import Dataset, file_sha256, iter_turns
from .ingestion import select_questions
from .metrics import (
    ChannelMetrics,
    MetricGroup,
    MetricValues,
    RetrievalMetrics,
    average_scores,
    markdown_metrics,
    percentile,
    score_ranking,
)
from .storage import EVAL_PRINCIPAL, eval_context

Channel = Literal["lexical", "vector", "hybrid"]
CHANNELS: dict[Channel, tuple[SearchChannel, ...]] = {
    "lexical": ("lexical",),
    "vector": ("vector",),
    "hybrid": ("lexical", "vector"),
}
RESULTS_DIRECTORY = Path("eval/results")


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    dataset: Dataset
    limit: int | None = None
    question_types: tuple[str, ...] = ()
    channels: tuple[Channel, ...] = ("lexical", "vector", "hybrid")
    k_list: tuple[int, ...] = (5, 10, 20)
    run_name: str = "longmemeval-retrieval"

    def __post_init__(self) -> None:
        if not self.k_list or any(k <= 0 for k in self.k_list):
            raise ValueError("k must contain at least one positive integer")
        if not self.channels or any(channel not in CHANNELS for channel in self.channels):
            raise ValueError("Channels must be lexical, vector or hybrid")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.run_name):
            raise ValueError("Run names must contain only letters, digits, '-' or '_'")


def _summary(rows: Sequence[MetricValues]) -> MetricGroup:
    return {"questions": len(rows), "scores": average_scores(rows)}


def _channel_metrics(groups: dict[str, list[MetricValues]]) -> ChannelMetrics:
    return {
        "overall": _summary([row for rows in groups.values() for row in rows]),
        "by_type": {name: _summary(rows) for name, rows in sorted(groups.items())},
    }


def _commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _write_json(path: Path, contents: object) -> None:
    path.write_text(json.dumps(contents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_retrieval(
    service: MemoryService,
    path: Path,
    options: RetrievalOptions,
    results_directory: Path = RESULTS_DIRECTORY,
) -> Path:
    start = perf_counter()
    digest = file_sha256(path)
    run_dir = results_directory / f"{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}-{options.run_name}"
    run_dir.mkdir(parents=True)

    scores: dict[Channel, dict[str, list[MetricValues]]] = {
        channel: defaultdict(list) for channel in options.channels
    }
    timings: dict[Channel, dict[str, list[float]]] = {
        channel: defaultdict(list) for channel in options.channels
    }
    questions = abstention = 0
    top_k = max(options.k_list)
    search_settings = service.settings.search
    # Search can return the full candidate pool to build a distinct session ranking.
    search_k = max(top_k, search_settings.n_lexical + search_settings.n_vector)
    with (run_dir / "per_question.jsonl").open("w", encoding="utf-8") as output:
        for question in select_questions(path, options.limit, options.question_types):
            questions += 1
            if question.abstention:
                abstention += 1
                output.write(
                    json.dumps(
                        {
                            "question_id": question.question_id,
                            "question_type": question.question_type,
                            "abstention": True,
                        }
                    )
                    + "\n"
                )
                continue
            bound = service.for_request(EVAL_PRINCIPAL, eval_context(question.question_id))
            if not bound.eval_has_question(options.dataset, digest):
                raise ValueError(
                    f"Question {question.question_id} has not been ingested for this dataset"
                )
            targets = {str(turn.memory_id) for turn in iter_turns(question) if turn.has_answer}
            answer_sessions = {
                f"{question.question_id}:{session}" for session in question.answer_session_ids
            }
            for channel in options.channels:
                response = bound.search(
                    SearchRequest(
                        query=question.question,
                        k=search_k,
                        channels=list(CHANNELS[channel]),
                        time_to=question.asked_at,
                        types=["turn"],
                        expand_relations=False,
                    )
                )
                ranked_sessions = list(
                    dict.fromkeys(hit.session_id for hit in response.hits if hit.session_id)
                )
                rank = [str(hit.memory_id) for hit in response.hits]
                values = score_ranking(
                    ranked_sessions, rank, answer_sessions, targets, options.k_list
                )
                scores[channel][question.question_type].append(values)
                for key, ms in response.timings_ms.model_dump().items():
                    timings[channel][key].append(ms)
                output.write(
                    json.dumps(
                        {
                            "question_id": question.question_id,
                            "question_type": question.question_type,
                            "channel": channel,
                            "scores": values,
                            "ranked_sessions": ranked_sessions[:top_k],
                            "hits": [
                                {
                                    "memory_id": str(hit.memory_id),
                                    "session_id": hit.session_id,
                                    "score": hit.score.model_dump(),
                                }
                                for hit in response.hits[:top_k]
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    metrics = RetrievalMetrics(
        questions=questions,
        abstention=abstention,
        channels={channel: _channel_metrics(groups) for channel, groups in scores.items()},
    )
    latency = {
        channel: {
            phase: {f"p{p}": percentile(samples, p / 100) for p in (50, 95, 99)}
            for phase, samples in phases.items()
        }
        for channel, phases in timings.items()
    }
    record_count = service.for_request(EVAL_PRINCIPAL, eval_context()).eval_record_count()
    embedding = service.settings.embedding
    manifest = {
        "benchmark": "longmemeval",
        "kind": "retrieval",
        "dataset": options.dataset,
        "dataset_file": path.name,
        "dataset_sha256": digest,
        "run_name": options.run_name,
        "git_commit": _commit(),
        "limit": options.limit,
        "question_types": options.question_types,
        "channels": options.channels,
        "k_list": options.k_list,
        "search_k": search_k,
        "embedding": {
            "provider": embedding.provider,
            "model": embedding.model,
            "dim": embedding.dim,
        },
        "tokenizer": {
            "implementation_sha256": file_sha256(Path(tokenizer.__file__)),
            "jieba": version("jieba"),
        },
        "search": search_settings.model_dump(),
        "record_count": record_count,
        "duration_seconds": perf_counter() - start,
    }
    _write_json(run_dir / "metrics.json", metrics)
    _write_json(run_dir / "latency.json", latency)
    (run_dir / "metrics.md").write_text(
        markdown_metrics([(options.run_name, metrics, options.k_list)]), encoding="utf-8"
    )
    _write_json(run_dir / "manifest.json", manifest)
    return run_dir
