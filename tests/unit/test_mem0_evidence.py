from importlib.util import find_spec
from pathlib import Path
from socket import socket

import pytest

from memfxxk.embedding import FakeEmbeddingProvider
from memfxxk.eval.evidence_backends import Mem0EvidenceBackend, render_evidence_turn
from memfxxk.eval.longmemeval.dataset import iter_questions, iter_turns

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "longmemeval-mini.json"


@pytest.mark.skipif(find_spec("mem0") is None, reason="requires optional eval-only Mem0 OSS")
def test_mem0_evidence_preserves_source_and_scope_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("The Mem0 evidence baseline attempted network access")

    monkeypatch.setattr(socket, "connect", reject_network)
    monkeypatch.setattr(socket, "connect_ex", reject_network)
    question, other = tuple(iter_questions(FIXTURE))
    expected = {
        str(turn.memory_id): (turn.official_session_id, render_evidence_turn(turn))
        for turn in iter_turns(question)
        if turn.event_time <= question.asked_at
    }
    other.question = question.question
    embedding = FakeEmbeddingProvider(model="offline-test", dim=128)

    with Mem0EvidenceBackend(embedding, tmp_path) as backend:
        with pytest.raises(ValueError, match="Prepare Mem0 question"):
            backend.search(question, 20)
        backend.prepare(question)
        backend.prepare(question)
        backend.prepare(other)
        hits = backend.search(question, 20)

        assert {hit.memory_id: (hit.session_id, hit.text) for hit in hits} == expected
        assert len(hits) == len(expected)
        assert {hit.session_id for hit in backend.search(other, 20)} == {"unrelated"}
        assert backend.manifest["infer"] is False
