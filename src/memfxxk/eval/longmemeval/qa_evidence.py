import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from memfxxk.embedding import _truncate
from memfxxk.eval.evidence_backends import render_evidence_turn
from memfxxk.text import TokenCounter

from .dataset import Question, Turn, iter_turns
from .suite import QuestionSuite

EVIDENCE_GROUPS = (
    "recent-turns",
    "memfxxk-lexical",
    "memfxxk-vector",
    "memfxxk-hybrid",
    "mem0-raw",
)


@dataclass(frozen=True, slots=True)
class EvidenceRun:
    suite: QuestionSuite
    source_manifest: dict[str, Any]
    rankings: dict[tuple[str, str], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class InjectedEvidence:
    text: str
    turn_ids: list[str]
    sessions: set[str]


def load_evidence_run(path: Path, suite: QuestionSuite, groups: tuple[str, ...]) -> EvidenceRun:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("kind") != "evidence"
        or manifest.get("dataset") != suite.dataset
        or manifest.get("dataset_sha256") != suite.dataset_sha256
        or tuple(manifest.get("question_ids", ())) != suite.question_ids
    ):
        raise ValueError("Evidence run must match the frozen question suite")

    required = {(question_id, group) for question_id in suite.question_ids for group in groups}
    rankings = {}
    with (path / "per_question.jsonl").open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            key = (row["question_id"], row["backend"])
            if key not in required:
                continue
            if key in rankings or any(not isinstance(item, str) for item in row["turn_ids"]):
                raise ValueError(f"Duplicate or unmapped evidence for {key[0]} / {key[1]}")
            rankings[key] = tuple(row["turn_ids"])
    if required != rankings.keys():
        raise ValueError("Evidence run is missing question and backend rankings")

    return EvidenceRun(suite, manifest, rankings)


def _render(turn: Turn, text: str) -> str:
    return f'<turn date="{turn.event_time.isoformat()}">\n{escape(text)}\n</turn>'


def inject_evidence(
    question: Question,
    ids: tuple[str, ...],
    k: int,
    budget: int,
    counter: TokenCounter,
) -> InjectedEvidence:
    cutoff = question.asked_at
    visible = {
        str(turn.memory_id): turn for turn in iter_turns(question) if turn.event_time <= cutoff
    }
    if any(turn_id not in visible for turn_id in ids):
        raise ValueError(f"{question.question_id}: evidence contains an out-of-scope turn")

    text = ""
    injected_ids = []
    sessions = set()
    for turn_id in tuple(dict.fromkeys(ids))[:k]:
        turn = visible[turn_id]
        prefix = f"{text}\n\n" if text else ""
        body = render_evidence_turn(turn)
        candidate = prefix + _render(turn, body)
        if counter.count(candidate) > budget:
            room = budget - counter.count(prefix + _render(turn, ""))
            if room <= 0:
                break
            excerpt = _truncate(body, room, counter)
            candidate = prefix + _render(turn, excerpt)
            while counter.count(candidate) > budget and excerpt:
                excerpt = excerpt[: len(excerpt) * 3 // 4]
                candidate = prefix + _render(turn, excerpt)
            if not excerpt:
                break
        text = candidate
        injected_ids.append(turn_id)
        sessions.add(turn.official_session_id)
    return InjectedEvidence(text, injected_ids, sessions)
