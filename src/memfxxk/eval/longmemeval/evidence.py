import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol

from memfxxk.domain.retrieval import SearchRequest
from memfxxk.service import BoundService, MemoryService

from .dataset import Question, Turn, iter_turns
from .ingestion import select_questions
from .metrics import MetricValues, markdown_metrics, percentile, score_ranking
from .retrieval import CHANNELS, Channel, _channel_metrics, _commit, _write_json
from .storage import EVAL_PRINCIPAL, eval_context
from .suite import QuestionSuite

K_LIST = (5, 10, 20)


class RetrievedHit(Protocol):
    @property
    def memory_id(self) -> str | None: ...


class EvidenceBackend(Protocol):
    @property
    def manifest(self) -> Mapping[str, object]: ...

    def prepare(self, question: Question) -> None: ...

    def search(self, question: Question, k: int) -> Sequence[RetrievedHit]: ...


@dataclass(frozen=True, slots=True)
class RankedEvidence:
    turn_ids: list[str | None]
    query_ms: float
    prepare_ms: float | None = None
    db_and_fusion_ms: float | None = None


def memfxxk_evidence(
    service: BoundService, question: Question, channel: Channel, candidates: int
) -> RankedEvidence:
    started = perf_counter()
    response = service.search(
        SearchRequest(
            query=question.question,
            k=candidates,
            channels=list(CHANNELS[channel]),
            time_to=question.asked_at,
            types=["turn"],
            expand_relations=False,
        )
    )
    return RankedEvidence(
        turn_ids=[str(hit.memory_id) for hit in response.hits],
        query_ms=(perf_counter() - started) * 1000,
        db_and_fusion_ms=response.timings_ms.total,
    )


def external_evidence(
    backend: EvidenceBackend, question: Question, candidates: int
) -> RankedEvidence:
    started = perf_counter()
    backend.prepare(question)
    searching = perf_counter()
    hits = backend.search(question, candidates)
    return RankedEvidence(
        turn_ids=[hit.memory_id for hit in hits],
        query_ms=(perf_counter() - searching) * 1000,
        prepare_ms=(searching - started) * 1000,
    )


def score_evidence(
    question: Question, visible: dict[str, Turn], evidence: RankedEvidence
) -> MetricValues:
    # An unmapped item can contain evidence. Its rank cannot be scored by source ID.
    sessions: list[str] = []
    turns: list[str] = []
    for turn_id in evidence.turn_ids:
        if turn_id is None:
            continue
        if turn_id not in visible:
            raise ValueError(f"{question.question_id}: backend returned an out-of-scope turn")
        sessions.append(visible[turn_id].official_session_id)
        turns.append(turn_id)
    scores = score_ranking(
        sessions,
        turns,
        set(question.answer_session_ids),
        {turn_id for turn_id, turn in visible.items() if turn.has_answer},
        K_LIST,
    )
    return dict.fromkeys(scores) if None in evidence.turn_ids else scores


def run_evidence(
    service: MemoryService,
    data_file: Path,
    suite: QuestionSuite,
    output: Path,
    run_name: str,
    external: Mapping[str, EvidenceBackend],
    *,
    channels: tuple[Channel, ...] = ("lexical", "vector", "hybrid"),
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_name):
        raise ValueError("Run names must contain only letters, digits, '-' or '_'")
    candidates = 100
    names = ("recent-turns", *(f"memfxxk-{channel}" for channel in channels), *external)
    if len(names) != len(set(names)):
        raise ValueError("Evidence backend names must be unique")
    scores: dict[str, dict[str, list[MetricValues]]] = {name: defaultdict(list) for name in names}
    timings: dict[str, list[RankedEvidence]] = {name: [] for name in names}
    counts: dict[str, Counter[str]] = {name: Counter() for name in names}
    started = perf_counter()
    run_dir = output / f"{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}-{run_name}"
    questions = select_questions(data_file, question_ids=suite.question_ids)
    run_dir.mkdir(parents=True)
    abstentions = 0
    eligible_turns = 0
    with (run_dir / "per_question.jsonl").open("w", encoding="utf-8") as observations:
        for question in questions:
            cutoff = question.asked_at
            visible = {
                str(turn.memory_id): turn
                for turn in iter_turns(question)
                if turn.event_time <= cutoff
            }
            abstentions += question.abstention
            eligible_turns += len(visible)
            bound = service.for_request(EVAL_PRINCIPAL, eval_context(question.question_id))
            if not bound.eval_has_question(suite.dataset, suite.dataset_sha256):
                raise ValueError(f"{question.question_id} must be ingested before evaluation")
            indexed, embedded = bound.eval_evidence_coverage(cutoff)
            if indexed != len(visible):
                raise ValueError(f"{question.question_id}: unexpected indexed turn count")
            if any(channel != "lexical" for channel in channels) and embedded != indexed:
                raise ValueError(
                    f"{question.question_id}: {embedded}/{indexed} turns embedded for the "
                    "current model. Finish indexing before comparing vector evidence."
                )
            start_recent = perf_counter()
            recent = sorted(
                reversed(visible), key=lambda turn_id: visible[turn_id].event_time, reverse=True
            )
            rankings = {
                "recent-turns": RankedEvidence(
                    recent[:candidates], (perf_counter() - start_recent) * 1000
                ),
                **{
                    f"memfxxk-{chan}": memfxxk_evidence(bound, question, chan, candidates)
                    for chan in channels
                },
                **{
                    name: external_evidence(backend, question, candidates)
                    for name, backend in external.items()
                },
            }
            for name, evidence in rankings.items():
                values = score_evidence(question, visible, evidence)
                if not question.abstention:
                    scores[name][question.question_type].append(values)
                timings[name].append(evidence)
                counts[name]["hits"] += len(evidence.turn_ids)
                counts[name]["unmapped"] += evidence.turn_ids.count(None)
                counts[name]["unmapped_queries"] += None in evidence.turn_ids
                counts[name]["empty_queries"] += not evidence.turn_ids
                observations.write(
                    json.dumps(
                        {
                            "question_id": question.question_id,
                            "question_type": question.question_type,
                            "abstention": question.abstention,
                            "backend": name,
                            "scores": None if question.abstention else values,
                            **asdict(evidence),
                        }
                    )
                    + "\n"
                )

    metrics = {
        "questions": len(suite.question_ids),
        "abstention": abstentions,
        "channels": {name: _channel_metrics(groups) for name, groups in scores.items()},
    }
    latency = {
        name: {
            stage: {f"p{p}": percentile(samples, p / 100) for p in (50, 95, 99)}
            for stage in ("query_ms", "prepare_ms", "db_and_fusion_ms")
            if (samples := [value for row in rows if (value := getattr(row, stage)) is not None])
        }
        for name, rows in timings.items()
    }
    settings = service.settings
    manifest = {
        "benchmark": "longmemeval",
        "kind": "evidence",
        **suite.model_dump(mode="json"),
        "git_commit": _commit(),
        "run_name": run_name,
        "candidate_turns": candidates,
        "eligible_turns": eligible_turns,
        "k_list": K_LIST,
        "memfxxk_channels": channels,
        "memfxxk_search": settings.search.model_dump(),
        "memfxxk_embedding": settings.embedding.model_dump(include={"provider", "model", "dim"}),
        "external_backends": {name: dict(backend.manifest) for name, backend in external.items()},
        "duration_seconds": perf_counter() - started,
    }
    _write_json(run_dir / "manifest.json", manifest)
    _write_json(run_dir / "metrics.json", metrics)
    _write_json(run_dir / "latency.json", latency)
    _write_json(run_dir / "diagnostics.json", {name: dict(stats) for name, stats in counts.items()})
    (run_dir / "metrics.md").write_text(
        markdown_metrics([(run_name, metrics, K_LIST)]), encoding="utf-8"
    )
    return run_dir
