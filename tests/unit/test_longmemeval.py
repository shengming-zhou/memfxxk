from datetime import UTC, datetime
from math import log2
from pathlib import Path

import pytest

from memfxxk.eval.longmemeval import dataset
from memfxxk.eval.longmemeval.dataset import (
    Message,
    iter_questions,
    iter_turns,
    pair_turns,
    parse_date,
)
from memfxxk.eval.longmemeval.metrics import score_ranking

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "longmemeval-mini.json"


def test_parse_official_date_as_utc() -> None:
    assert parse_date("2023/05/20 (Sat) 02:21") == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_date("2023/02/31 (Thu) 01:00")


def test_pair_turns_leaves_unmatched_users_as_turns() -> None:
    messages = [
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
        Message(role="user", content="four"),
    ]
    assert list(pair_turns(messages)) == [
        (messages[0], messages[1]),
        (messages[2],),
        (messages[3],),
    ]


def test_streamed_fixture_preserves_labels_across_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dataset, "_CHUNK_SIZE", 17)
    multi, abstention = iter_questions(FIXTURE)
    assert multi.question_id == "mini_multi"
    assert not multi.abstention
    assert abstention.abstention
    assert [(turn.session_id, turn.turn_index, turn.has_answer) for turn in iter_turns(multi)] == [
        ("mini_multi:alpha_session", 0, True),
        ("mini_multi:zulu_session", 0, True),
        ("mini_multi:future_session", 0, False),
    ]


def test_ranking_metrics_deduplicate_sessions_and_discount_rank() -> None:
    metrics = score_ranking(
        ["wrong", "answer_a", "answer_a", "answer_b"],
        ["wrong_turn", "turn_a", "another_turn", "turn_b"],
        {"answer_a", "answer_b"},
        {"turn_a", "turn_b"},
        (2, 3, 4),
    )
    assert metrics["session_recall_any@2"] == 1.0
    assert metrics["session_recall_all@2"] == 0.0
    assert metrics["session_recall_all@3"] == 1.0
    assert metrics["turn_recall_all@3"] == 0.0
    assert metrics["turn_recall_all@4"] == 1.0
    assert metrics["session_ndcg@3"] == pytest.approx((1 / log2(3) + 0.5) / (1 + 1 / log2(3)))
