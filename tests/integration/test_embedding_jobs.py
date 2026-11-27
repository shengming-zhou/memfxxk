from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from memfxxk.db import embedding_cache, jobs, record_embeddings, records
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.events import IngestEvent
from memfxxk.embedding import CachedEmbedder, FakeEmbeddingProvider
from memfxxk.jobs import Worker, drain
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


class RecordingProvider(FakeEmbeddingProvider):
    def __init__(self, before_embed: Callable[[], None] | None = None) -> None:
        super().__init__(model="test-model", dim=8)
        self.batches: list[list[str]] = []
        self.before_embed = before_embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(texts.copy())
        if self.before_embed is not None:
            self.before_embed()
        return super().embed(texts)


@pytest.fixture
def write_turns(
    service: MemoryService, make_ctx: Callable[..., RequestContext]
) -> Callable[..., None]:
    ctx = make_ctx(user="u1", project="p1")
    bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)

    def write(*contents: str) -> None:
        bound.ingest(
            [
                IngestEvent(kind="message", role="user", content=content, session_id="embed")
                for content in contents
            ]
        )

    return write


def test_drain_embeds_batch_and_reuses_cached_content(
    service: MemoryService, engine: Engine, write_turns: Callable[..., None]
) -> None:
    provider = RecordingProvider()
    embedder = CachedEmbedder(engine, provider)
    write_turns("alpha", "beta")

    assert drain(service, embedder=embedder, kinds={"embed_record"}) == 2
    assert len(provider.batches) == 1
    assert len(provider.batches[0]) == 2

    write_turns("alpha")
    assert drain(service, embedder=embedder, kinds={"embed_record"}) == 1
    assert len(provider.batches) == 1

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(embedding_cache)) == 2
        assert connection.scalar(select(func.count()).select_from(record_embeddings)) == 3
        assert connection.scalars(select(record_embeddings.c.dim).distinct()).all() == [8]


@pytest.mark.parametrize("when", ["before_claim", "during_embedding"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_stale_job_never_writes_embedding(
    service: MemoryService,
    engine: Engine,
    write_turns: Callable[..., None],
    when: str,
    change: str,
) -> None:
    write_turns("old content")
    with engine.connect() as connection:
        record_id: UUID = connection.execute(select(records.c.id)).scalar_one()

    def change_record() -> None:
        with engine.begin() as connection:
            if change == "edit":
                connection.execute(
                    records.update()
                    .where(records.c.id == record_id)
                    .values(content="updated content", generation=records.c.generation + 1)
                )
            else:
                connection.execute(records.delete().where(records.c.id == record_id))

    if when == "before_claim":
        change_record()
    provider = RecordingProvider(change_record if when == "during_embedding" else None)

    assert drain(service, embedder=CachedEmbedder(engine, provider)) == 1
    assert bool(provider.batches) == (when == "during_embedding")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(record_embeddings)) == 0
        assert (
            connection.scalar(select(jobs.c.status).where(jobs.c.kind == "embed_record")) == "done"
        )


def test_job_retries_after_provider_failure_and_goes_dead(
    service: MemoryService, engine: Engine, write_turns: Callable[..., None]
) -> None:
    def fail_embedding() -> None:
        raise RuntimeError("Provider failed")

    write_turns("retry this")
    worker = Worker(service, CachedEmbedder(engine, RecordingProvider(fail_embedding)))
    with engine.connect() as connection:
        initial = connection.execute(select(jobs)).mappings().one()

    for attempt in range(1, initial["max_attempts"] + 1):
        assert worker.run_once() == 1
        with engine.begin() as connection:
            result = connection.execute(select(jobs)).mappings().one()
            assert result["attempts"] == attempt
            assert result["last_error"] == "Provider failed"
            assert result["run_after"] >= result["updated_at"] + timedelta(
                seconds=min(2**attempt, 600)
            )
            if attempt < initial["max_attempts"]:
                assert result["status"] == "queued"
                connection.execute(jobs.update().values(run_after=func.now()))
            else:
                assert result["status"] == "dead"
    assert worker.run_once() == 0


def test_rebuild_during_embedding_keeps_new_job_queued(
    service: MemoryService, engine: Engine, make_ctx: Callable[..., RequestContext]
) -> None:
    ctx = make_ctx(user="u1", project="p1")
    bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    event = IngestEvent(
        kind="message", role="user", content="start of turn", session_id="same", turn_id="a"
    )
    bound.ingest([event])

    def rebuild_turn() -> None:
        bound.ingest([event.model_copy(update={"role": "assistant", "content": "end of turn"})])

    provider = RecordingProvider(rebuild_turn)
    worker = Worker(service, CachedEmbedder(engine, provider))
    assert worker.run_once() == 1
    with engine.connect() as connection:
        assert connection.execute(select(jobs.c.status)).scalar_one() == "queued"
        assert connection.scalar(select(func.count()).select_from(record_embeddings)) == 0

    provider.before_embed = None
    assert worker.run_once() == 1
    assert len(provider.batches) == 2
    assert "end of turn" in provider.batches[1][0]
    with engine.connect() as connection:
        assert (
            connection.scalar(select(jobs.c.status).where(jobs.c.kind == "embed_record")) == "done"
        )
        assert connection.scalar(select(func.count()).select_from(record_embeddings)) == 1
