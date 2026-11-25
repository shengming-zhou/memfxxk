from uuid import UUID

import pytest

from memfxxk.config import SearchSettings
from memfxxk.retrieval import fuse
from memfxxk.retrieval.channels import Candidate


def test_important_only_candidate_interpolates_a_vector_rank() -> None:
    best = Candidate(UUID(int=1), 0, 0.9)
    second = Candidate(UUID(int=2), 0, 0.7)
    important = Candidate(UUID(int=3), 0, 0.8)
    settings = SearchSettings(lexical_weight=2, vector_weight=1, rrf_k=10, important_boost=1.5)

    scores = fuse([best], [best, second], [important], settings)

    assert scores[best.id].rrf == pytest.approx(3 / 11)
    assert scores[second.id].lexical_rank is None
    assert scores[important.id].lexical_rank is None
    assert scores[important.id].vector_rank == pytest.approx(1.5)
    assert scores[important.id].important
    assert scores[important.id].final == pytest.approx(1.5 / 11.5)
