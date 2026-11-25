from collections import defaultdict
from collections.abc import Hashable, Iterable, Sequence
from math import floor, log2
from statistics import mean
from typing import TypedDict

MetricValues = dict[str, float | None]


class MetricGroup(TypedDict):
    questions: int
    scores: MetricValues


class ChannelMetrics(TypedDict):
    overall: MetricGroup
    by_type: dict[str, MetricGroup]


class RetrievalMetrics(TypedDict):
    questions: int
    abstention: int
    channels: dict[str, ChannelMetrics]


_METRICS = (
    ("session_recall_any", "Session any"),
    ("session_recall_all", "Session all"),
    ("turn_recall_any", "Turn any"),
    ("turn_recall_all", "Turn all"),
    ("session_ndcg", "Session nDCG"),
)


def score_ranking(
    ranked_sessions: Sequence[str],
    ranked_turns: Sequence[str],
    answer_sessions: set[str],
    answer_turns: set[str],
    k_list: tuple[int, ...],
) -> MetricValues:
    sessions = list(dict.fromkeys(ranked_sessions))
    turns = list(dict.fromkeys(ranked_turns))
    metrics: MetricValues = {}
    for k in k_list:
        for name, ranking, targets in (
            ("session", sessions, answer_sessions),
            ("turn", turns, answer_turns),
        ):
            observed = set(ranking[:k])
            metrics[f"{name}_recall_any@{k}"] = float(bool(targets & observed)) if targets else None
            metrics[f"{name}_recall_all@{k}"] = float(targets <= observed) if targets else None
        dcg = sum(
            1 / log2(i + 2) for i, session in enumerate(sessions[:k]) if session in answer_sessions
        )
        ideal_dcg = sum(1 / log2(i + 2) for i in range(min(k, len(answer_sessions))))
        metrics[f"session_ndcg@{k}"] = dcg / ideal_dcg if ideal_dcg else None
    return metrics


def average_scores(scores: Iterable[MetricValues]) -> MetricValues:
    values: dict[str, list[float]] = defaultdict(list)
    for row in scores:
        for key, value in row.items():
            if value is not None:
                values[key].append(value)
            else:
                values.setdefault(key, [])
    return {key: mean(samples) if samples else None for key, samples in values.items()}


def percentile(samples: Sequence[float], quantile: float) -> float:
    ordered = sorted(samples)
    index = (len(ordered) - 1) * quantile
    lower = floor(index)
    fraction = index - lower
    if not fraction:
        return ordered[lower]
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


def markdown_metrics(runs: Iterable[tuple[str, RetrievalMetrics, tuple[int, ...]]]) -> str:
    rows: list[list[Hashable]] = []
    for name, metrics, k_list in runs:
        for channel, channel_metrics in metrics["channels"].items():
            groups = [("all", channel_metrics["overall"]), *channel_metrics["by_type"].items()]
            for question_type, summary in groups:
                for k in k_list:
                    values = [summary["scores"].get(f"{key}@{k}") for key, _ in _METRICS]
                    rows.append(
                        [
                            name,
                            channel,
                            question_type,
                            k,
                            summary["questions"],
                            *("-" if value is None else f"{value:.4f}" for value in values),
                        ]
                    )
    headings = [
        "Run",
        "Channel",
        "Question type",
        "k",
        "Questions",
        *(label for _, label in _METRICS),
    ]
    return "\n".join(_table_row(row) for row in (headings, ["---"] * len(headings), *rows)) + "\n"


def _table_row(values: Sequence[Hashable]) -> str:
    return (
        "| "
        + " | ".join(str(value).replace("|", r"\|").replace("\n", " ") for value in values)
        + " |"
    )
