from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Engine, select, text

from memfxxk import context, documents, ingest, manage, retrieval, sessions
from memfxxk.config import Settings
from memfxxk.db import records, transaction
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.context import BuildContextRequest, BundleMetadata, ContextBundle
from memfxxk.domain.documents import Document, DocumentInfo, DocumentRevision, PutDocumentRequest
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.domain.manage import (
    ForgetMode,
    ForgetResult,
    LinkRequest,
    LinkResult,
    ListRequest,
    MemoryPage,
    MemoryVersion,
    PatchRequest,
    RememberRequest,
)
from memfxxk.domain.retrieval import MemoryResponse, SearchRequest, SearchResponse
from memfxxk.domain.sessions import PutSessionState, SessionState
from memfxxk.embedding import ensure_embedding_index
from memfxxk.jobs import enqueue
from memfxxk.scope import ScopeError

if TYPE_CHECKING:
    from memfxxk.eval.longmemeval.dataset import Dataset, Question


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

    def search(self, request: SearchRequest) -> SearchResponse:
        return retrieval.search(self, request)

    def get_memory(
        self, memory_id: UUID, version: int | None = None, window: int = 0
    ) -> MemoryResponse:
        return retrieval.get_memory(self, memory_id, version, window)

    def remember(self, request: RememberRequest) -> MemoryVersion:
        return manage.remember(self, request)

    def patch_memory(self, memory_id: UUID, request: PatchRequest) -> MemoryVersion:
        return manage.patch_memory(self, memory_id, request)

    def link(self, request: LinkRequest) -> LinkResult:
        return manage.link(self, request)

    def unlink(self, relation_id: UUID) -> None:
        manage.unlink(self, relation_id)

    def forget(self, memory_id: UUID, mode: ForgetMode) -> ForgetResult:
        return manage.forget(self, memory_id, mode)

    def forget_event(self, event_id: UUID) -> ForgetResult:
        return manage.forget_event(self, event_id)

    def list_memories(self, filters: ListRequest | None = None) -> MemoryPage:
        return manage.list_memories(self, filters or ListRequest())

    def put_document(self, path: str, request: PutDocumentRequest) -> Document:
        return documents.put_document(self, path, request)

    def get_document(self, path: str, revision: int | None = None) -> Document:
        return documents.get_document(self, path, revision)

    def list_documents(self) -> list[DocumentInfo]:
        return documents.list_documents(self)

    def list_revisions(self, path: str) -> list[DocumentRevision]:
        return documents.list_revisions(self, path)

    def delete_document(self, path: str) -> None:
        documents.delete_document(self, path)

    def get_session(self, session_id: str) -> SessionState:
        return sessions.get_session(self, session_id)

    def put_session_state(self, session_id: str, request: PutSessionState) -> SessionState:
        return sessions.put_session_state(self, session_id, request)

    def build_context(self, request: BuildContextRequest) -> ContextBundle:
        return context.build_context(self, request)

    def get_bundle(self, bundle_id: UUID) -> BundleMetadata:
        return context.get_bundle(self, bundle_id)

    def eval_ingest(self, question: "Question", dataset: "Dataset", sha256: str) -> bool:
        from memfxxk.eval.longmemeval.storage import ingest_question

        return ingest_question(self, question, dataset, sha256)

    def eval_has_question(self, dataset: "Dataset", sha256: str) -> bool:
        from memfxxk.eval.longmemeval.storage import has_question

        return has_question(self, dataset, sha256)

    def eval_evidence_coverage(self, cutoff: datetime) -> tuple[int, int]:
        from memfxxk.eval.longmemeval.storage import evidence_coverage

        return evidence_coverage(self, cutoff)

    def eval_reset(self) -> int:
        from memfxxk.eval.longmemeval.storage import reset_eval

        return reset_eval(self)

    def eval_record_count(self) -> int:
        from memfxxk.eval.longmemeval.storage import count_records

        return count_records(self)
