from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Collection
from datetime import timedelta
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from memfxxk.db import compactions, context_bundles, jobs, record_embeddings, records, transaction
from memfxxk.ids import new_id

from .queue import Job, claim, complete, fail, owned_claim

if TYPE_CHECKING:
    from memfxxk.embedding import CachedEmbedder
    from memfxxk.service import MemoryService

logger = logging.getLogger(__name__)


def embed_records(engine: Engine, embedder: CachedEmbedder, batch: list[Job]) -> None:
    requests = [(job, UUID(job.payload["record_id"]), job.payload["generation"]) for job in batch]
    with engine.connect() as conn:
        rows = conn.execute(
            select(records.c.id, records.c.generation, records.c.content).where(
                records.c.id.in_([record_id for _, record_id, _ in requests]),
                records.c.status == "active",
            )
        ).all()
    snapshots = {(row.id, row.generation): row.content for row in rows}
    pending = [
        (job, record_id, generation, snapshots[record_id, generation])
        for job, record_id, generation in requests
        if (record_id, generation) in snapshots
    ]
    if not pending:
        return

    vectors = embedder.embed([content for _, _, _, content in pending])
    for (job, record_id, generation, _), vector in zip(pending, vectors, strict=True):
        with transaction(engine) as conn:
            # Match ingest's record-before-job lock order and recheck after slow embedding.
            current = conn.execute(
                select(records.c.id)
                .where(
                    records.c.id == record_id,
                    records.c.generation == generation,
                    records.c.status == "active",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current is None:
                continue
            lease = conn.execute(
                select(jobs.c.id).where(owned_claim(job)).with_for_update()
            ).scalar_one_or_none()
            if lease is None:
                continue
            statement = insert(record_embeddings).values(
                record_id=record_id,
                model=embedder.model,
                dim=embedder.dim,
                embedding=vector,
            )
            conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[record_embeddings.c.record_id, record_embeddings.c.model],
                    set_={
                        "dim": statement.excluded.dim,
                        "embedding": statement.excluded.embedding,
                        "created_at": func.now(),
                    },
                )
            )


class Worker:
    def __init__(
        self,
        service: MemoryService,
        embedder: CachedEmbedder | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.engine = service.engine
        self.worker_id = worker_id or str(new_id())
        self.bundle_retention = timedelta(days=service.settings.bundle_retention_days)
        self.draft_retention = timedelta(hours=service.settings.m2.draft_retention_hours)
        from memfxxk.embedding import CachedEmbedder, make_embedding_provider

        if embedder is None:
            embedder = CachedEmbedder(service.engine, make_embedding_provider(service.settings))

        def handle_embed(batch: list[Job]) -> None:
            models: dict[str, list[Job]] = defaultdict(list)
            for job in batch:
                models[job.payload.get("model", embedder.model)].append(job)
            for model, group in models.items():
                target = embedder
                if model != embedder.model:
                    config = service.settings.embedding.model_copy(update={"model": model})
                    settings = service.settings.model_copy(update={"embedding": config})
                    target = CachedEmbedder(self.engine, make_embedding_provider(settings))
                embed_records(self.engine, target, group)

        self.handlers: dict[str, Callable[[list[Job]], None]] = {"embed_record": handle_embed}
        if service.settings.m2.extract_facts:
            from memfxxk.eval.llm import ChatClient
            from memfxxk.m2.facts import extract

            def handle_facts(batch: list[Job]) -> None:
                with ChatClient(service.settings.m2.model, cache_dir=None) as model:
                    for job in batch:
                        extract(service, job, model)

            self.register("extract_facts", handle_facts)

    def register(self, kind: str, handler: Callable[[list[Job]], None]) -> None:
        self.handlers[kind] = handler

    def run_once(self, kinds: Collection[str] | None = None, limit: int = 32) -> int:
        eligible = self.handlers.keys() if kinds is None else self.handlers.keys() & set(kinds)
        with transaction(self.engine) as conn:
            claimed = claim(conn, self.worker_id, eligible, limit)

        batches: dict[str, list[Job]] = defaultdict(list)
        for job in claimed:
            batches[job.kind].append(job)
        for kind, batch in batches.items():
            try:
                self.handlers[kind](batch)
            except Exception as error:
                for job in batch:
                    with transaction(self.engine) as conn:
                        fail(conn, job, error)
            else:
                for job in batch:
                    with transaction(self.engine) as conn:
                        complete(conn, job)
        return len(claimed)

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        next_cleanup = 0.0
        while not stop.is_set():
            try:
                if monotonic() >= next_cleanup:
                    self.cleanup_bundles()
                    next_cleanup = monotonic() + 600
                if self.run_once() == 0:
                    stop.wait(0.5)
            except OperationalError:
                logger.warning("Database temporarily unavailable while processing memory jobs")
                stop.wait(1)

    def cleanup_bundles(self) -> None:
        with transaction(self.engine) as connection:
            connection.execute(
                context_bundles.delete().where(
                    context_bundles.c.created_at < func.now() - self.bundle_retention
                )
            )
            connection.execute(
                compactions.delete().where(
                    compactions.c.committed_revision.is_(None),
                    compactions.c.created_at < func.now() - self.draft_retention,
                )
            )


def drain(
    service: MemoryService,
    kinds: Collection[str] | None = None,
    max_rounds: int = 100,
    *,
    embedder: CachedEmbedder | None = None,
) -> int:
    """Process due jobs synchronously, leaving delayed retries scheduled."""
    worker = Worker(service, embedder)
    total = 0
    for _ in range(max_rounds):
        count = worker.run_once(kinds)
        if count == 0:
            break
        total += count
    return total
