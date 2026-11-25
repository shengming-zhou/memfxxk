from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from sqlalchemy.engine import Engine

from memfxxk.embedding import CachedEmbedder, EmbeddingProvider, make_embedding_provider
from memfxxk.jobs import drain
from memfxxk.service import MemoryService

from .dataset import Dataset, Question, file_sha256, iter_questions
from .storage import EVAL_PRINCIPAL, eval_context


@dataclass(frozen=True, slots=True)
class IngestSummary:
    inserted: int
    skipped: int
    cache_hits: int
    cache_misses: int

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total else 0.0


class CountingProvider:
    def __init__(self, provider: EmbeddingProvider) -> None:
        self.provider = provider
        self.model = provider.model
        self.dim = provider.dim
        self.requested_texts = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.requested_texts += len(texts)
        return self.provider.embed(texts)


class CountingEmbedder(CachedEmbedder):
    def __init__(self, engine: Engine, provider: EmbeddingProvider) -> None:
        self.counted_provider = CountingProvider(provider)
        super().__init__(engine, self.counted_provider)
        self.requested_texts = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.requested_texts += len(texts)
        return super().embed(texts)


def select_questions(
    path: Path, limit: int | None = None, question_types: tuple[str, ...] = ()
) -> Iterator[Question]:
    questions = (
        question
        for question in iter_questions(path)
        if not question_types or question.question_type in question_types
    )
    return islice(questions, limit)


def ingest_dataset(
    service: MemoryService,
    path: Path,
    dataset: Dataset,
    *,
    limit: int | None = None,
    question_types: tuple[str, ...] = (),
    reset: bool = False,
) -> IngestSummary:
    digest = file_sha256(path)
    if reset:
        service.for_request(EVAL_PRINCIPAL, eval_context()).eval_reset()
    inserted = skipped = 0
    for question in select_questions(path, limit, question_types):
        bound = service.for_request(EVAL_PRINCIPAL, eval_context(question.question_id))
        if bound.eval_ingest(question, dataset, digest):
            inserted += 1
        else:
            skipped += 1
    embedder = CountingEmbedder(service.engine, make_embedding_provider(service.settings))
    drain(service, kinds={"embed_record"}, max_rounds=100_000, embedder=embedder)
    misses = embedder.counted_provider.requested_texts
    return IngestSummary(inserted, skipped, max(0, embedder.requested_texts - misses), misses)
