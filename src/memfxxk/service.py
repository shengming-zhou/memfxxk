from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, select, text

from memfxxk import ingest
from memfxxk.config import Settings
from memfxxk.db import records, transaction
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.embedding import ensure_embedding_index
from memfxxk.jobs import enqueue
from memfxxk.scope import ScopeError


@dataclass(frozen=True, slots=True)
class MemoryService:
    engine: Engine
    settings: Settings

    def for_request(self, principal: Principal, ctx: RequestContext) -> "BoundService":
        return BoundService(self.engine, self.settings, principal, ctx)

    def ensure_index(self) -> str:
        config = self.settings.embedding
        with transaction(self.engine) as connection:
            return ensure_embedding_index(connection, config.model, config.dim)

    def reembed(self, model: str | None = None) -> int:
        target_model = self.settings.embedding.model if model is None else model
        with transaction(self.engine) as connection:
            current = connection.execute(
                select(records.c.id, records.c.generation).where(records.c.is_current)
            )
            return sum(
                enqueue(
                    connection,
                    "embed_record",
                    {"record_id": str(row.id), "generation": row.generation, "model": target_model},
                    dedupe_key=f"reembed:{row.id}:{target_model}",
                )
                is not None
                for row in current
            )


@dataclass(frozen=True, slots=True)
class BoundService:
    engine: Engine
    settings: Settings
    principal: Principal
    ctx: RequestContext

    def __post_init__(self) -> None:
        if self.principal.org_id != self.ctx.org_id:
            raise ScopeError("The request context must belong to the principal's organization")

    def check_database(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def ingest(self, batch: list[IngestEvent]) -> list[IngestResult]:
        with transaction(self.engine) as connection:
            return ingest.ingest(connection, self.ctx, batch)

    def get_event(self, event_id: UUID) -> Event:
        with self.engine.connect() as connection:
            return ingest.get_event(connection, self.ctx, event_id)

    def list_session_events(
        self,
        session_id: str,
        from_seq: int | None = None,
        to_seq: int | None = None,
        limit: int = 100,
    ) -> list[Event]:
        with self.engine.connect() as connection:
            return ingest.list_session_events(
                connection, self.ctx, session_id, from_seq, to_seq, limit
            )
