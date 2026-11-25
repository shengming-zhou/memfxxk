from bisect import bisect_left
from typing import Protocol
from uuid import UUID

from memfxxk.config import SearchSettings
from memfxxk.domain.retrieval import ScoreDetails, SearchHit, SearchRequest
from memfxxk.retrieval.channels import Candidate


class Reranker(Protocol):
    def rerank(self, request: SearchRequest, hits: list[SearchHit]) -> list[SearchHit]: ...


class NoopReranker:
    def rerank(self, request: SearchRequest, hits: list[SearchHit]) -> list[SearchHit]:
        return hits


def _interpolated_rank(similarities: list[float], similarity: float) -> float:
    """Compare unreturned important candidates to the observed vector ranking.

    Negative similarities are sorted ascending. Equal similarities share the earliest observed
    rank. Between unequal neighbors use linear interpolation of the adjacent one-based ranks.
    Above the best candidate use rank 1, below the last use the next rank.
    """
    key = -similarity
    index = bisect_left(similarities, key)
    if index == 0:
        return 1.0
    if index == len(similarities) or similarities[index] == key:
        return float(index + 1)
    upper = similarities[index - 1]
    lower = similarities[index]
    return index + (key - upper) / (lower - upper)


def fuse(
    lexical: list[Candidate],
    vector: list[Candidate],
    important: list[Candidate],
    settings: SearchSettings,
) -> dict[UUID, ScoreDetails]:
    lexical_ranks = {
        candidate.id: (rank, candidate.score) for rank, candidate in enumerate(lexical, 1)
    }
    vector_ranks = {
        candidate.id: (rank, candidate.score) for rank, candidate in enumerate(vector, 1)
    }
    important_scores = {candidate.id: candidate.score for candidate in important}
    similarities = [-candidate.score for candidate in vector]
    scores = {}
    for record_id in lexical_ranks.keys() | vector_ranks.keys() | important_scores.keys():
        lexical_rank, lexical_score = lexical_ranks.get(record_id, (None, None))
        vector_rank, vector_sim = vector_ranks.get(record_id, (None, None))
        is_important = record_id in important_scores
        if is_important and lexical_rank is None and vector_rank is None:
            vector_sim = important_scores[record_id]
            vector_rank = _interpolated_rank(similarities, vector_sim)
        rrf = 0.0
        if lexical_rank is not None:
            rrf += settings.lexical_weight / (settings.rrf_k + lexical_rank)
        if vector_rank is not None:
            rrf += settings.vector_weight / (settings.rrf_k + vector_rank)
        boost = settings.important_boost if is_important else 1.0
        scores[record_id] = ScoreDetails(
            lexical_rank=lexical_rank,
            lexical_score=lexical_score,
            vector_rank=vector_rank,
            vector_sim=vector_sim,
            important=is_important,
            rrf=rrf,
            boost=boost,
            final=rrf * boost,
        )
    return scores
