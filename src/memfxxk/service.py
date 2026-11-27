from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Engine, select, text

from memfxxk import context, documents, ingest, manage, retrieval, sessions
from memfxxk.config import Settings
from memfxxk.db import records, transaction
from memfxxk.domain import Actor, Principal, RequestContext
from memfxxk.domain.blocks import (
    BlockEditResult,
    BlockProposal,
    CreateBlock,
    EditBlock,
    MemoryBlock,
)
from memfxxk.domain.context import (
    BuildContextRequest,
    BundleMetadata,
    CommittedCompaction,
    CompactionDraft,
    ContextBundle,
    PrepareCompaction,
)
from memfxxk.domain.documents import Document, DocumentInfo, DocumentRevision, PutDocumentRequest
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.domain.facts import FactProposal
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
    SaveLessonRequest,
)
from memfxxk.domain.notes import NoteHistoryEntry, NoteProjection, RollbackNote
from memfxxk.domain.retrieval import MemoryResponse, SearchRequest, SearchResponse
from memfxxk.domain.sessions import PutSessionState, SessionState
from memfxxk.domain.workspace import (
    ImportWorkspaceRequest,
    WorkspaceImportResult,
    WorkspaceSnapshot,
)
from memfxxk.embedding import ensure_embedding_index
from memfxxk.idempotency import idempotency_namespace
from memfxxk.jobs import enqueue
from memfxxk.scope import ScopeError

if TYPE_CHECKING:
    from memfxxk.eval.longmemeval.dataset import Dataset, Question
    from memfxxk.llm.runner import FakeLLM


@dataclass(frozen=True, slots=True)
class MemoryService:
    engine: Engine
    settings: Settings
    fake_llm: "FakeLLM | None" = None

    def for_request(self, principal: Principal, ctx: RequestContext) -> "BoundService":
        return BoundService(self.engine, self.settings, principal, ctx, self.fake_llm)

    def ensure_index(self) -> str:
        config = self.settings.embedding
        with transaction(self.engine) as connection:
            index = ensure_embedding_index(connection, config.model, config.dim)
        if self.settings.search.vector_backend == "qdrant":
            from memfxxk.qdrant import QdrantClient

            client = QdrantClient(self.settings.qdrant)
            client.ensure(client.collection(config.model, config.dim), config.dim)
        return index

    def requeue_qdrant(self) -> int:
        from memfxxk.qdrant import QdrantClient, requeue_endpoint

        with transaction(self.engine) as connection:
            return requeue_endpoint(connection, QdrantClient(self.settings.qdrant))

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
    fake_llm: "FakeLLM | None" = None

    def __post_init__(self) -> None:
        if self.principal.org_id != self.ctx.org_id:
            raise ScopeError("The request context must belong to the principal's organization")
        grant = self.principal.project_grant
        if grant is not None:
            bound = {
                "user_id": grant.user_id,
                "project_id": grant.project_id,
                "agent_id": grant.agent_id,
            }
            if any(getattr(self.ctx, key) not in (None, value) for key, value in bound.items()):
                raise ScopeError("The request context must match the project credential")
            object.__setattr__(
                self,
                "ctx",
                RequestContext(
                    org_id=self.principal.org_id,
                    **bound,
                    actor=Actor(type="user", id=grant.user_id),
                ),
            )

    @property
    def write_project(self) -> str | None:
        grant = self.principal.project_grant
        return grant.project_id if grant is not None else None

    @property
    def idempotency_namespace(self) -> str:
        return idempotency_namespace(self.principal.project_grant)

    def require_write(self, scope_drop: Collection[str] = ()) -> None:
        grant = self.principal.project_grant
        if grant is not None:
            if grant.access != "write":
                raise ScopeError("The project credential is read-only")
            if "project_id" in scope_drop:
                raise ScopeError("Project credentials must retain the project scope")

    def require_resource_write(self, project_id: str | None) -> None:
        if self.write_project is not None and project_id != self.write_project:
            raise ingest.NotFound

    def hard_delete_denial(self) -> str | None:
        if self.principal.role == "project":
            return "Project credentials cannot permanently delete data"
        if self.principal.role == "host" and not self.settings.allow_host_hard_delete:
            return "Hard deletion is disabled for host keys"
        return None

    def check_database(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def ingest(self, batch: list[IngestEvent]) -> list[IngestResult]:
        self.require_write({dimension for event in batch for dimension in event.scope_drop})
        with transaction(self.engine) as connection:
            results = ingest.ingest(
                connection,
                self.ctx,
                batch,
                write_project=self.write_project,
                namespace=self.idempotency_namespace,
            )
            kinds = []
            if self.settings.fact_strategy != "off":
                kinds.append("extract_facts")
            if self.settings.strategies.graph == "graphiti":
                kinds.append("build_entity_graph")
            if self.settings.strategies.blocks == "letta" and self.settings.sleeptime.every_turns:
                kinds.append("sleeptime_consolidate")
            if self.settings.strategies.tiers == "memoryos":
                kinds.append("tier_project")
            if kinds:
                from memfxxk.m2.facts import enqueue_closed_turns

                enqueue_closed_turns(connection, self.ctx, batch, results, kinds)
            return results

    def list_fact_proposals(self, limit: int = 50) -> list[FactProposal]:
        from memfxxk.m2.facts import list_pending

        return list_pending(self, limit)

    def review_fact(self, proposal_id: UUID, accept: bool) -> FactProposal:
        from memfxxk.m2.facts import review

        self.require_write()
        return review(self, proposal_id, accept)

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

    def create_block(self, request: CreateBlock) -> MemoryBlock:
        from memfxxk.m2.blocks import create

        return create(self, request)

    def get_block(self, block_id: UUID) -> MemoryBlock:
        from memfxxk.m2.blocks import get

        return get(self, block_id)

    def list_blocks(self) -> list[MemoryBlock]:
        from memfxxk.m2.blocks import list_blocks

        return list_blocks(self)

    def edit_block(self, block_id: UUID, request: EditBlock) -> BlockEditResult:
        from memfxxk.m2.blocks import edit

        self.require_write()
        return edit(self, block_id, request)

    def list_block_proposals(self) -> list[BlockProposal]:
        from memfxxk.m2.blocks import list_proposals

        return list_proposals(self)

    def review_block(self, proposal_id: UUID, accept: bool) -> BlockProposal:
        from memfxxk.m2.blocks import review

        self.require_write()
        return review(self, proposal_id, accept)

    def rollback_note(self, memory_id: UUID, request: RollbackNote) -> NoteProjection:
        from memfxxk.m2.amem import rollback

        self.require_write()
        return rollback(self, memory_id, request)

    def note_history(self, memory_id: UUID, limit: int = 50) -> list[NoteHistoryEntry]:
        from memfxxk.m2.amem import history

        return history(self, memory_id, limit)

    def remember(self, request: RememberRequest) -> MemoryVersion:
        self.require_write(request.scope_drop)
        return manage.remember(self, request)

    def save_lesson(self, request: SaveLessonRequest) -> MemoryVersion:
        self.require_write()
        return manage.save_lesson(self, request)

    def patch_memory(self, memory_id: UUID, request: PatchRequest) -> MemoryVersion:
        self.require_write(request.mark_scope_drop)
        return manage.patch_memory(self, memory_id, request)

    def link(self, request: LinkRequest) -> LinkResult:
        self.require_write()
        return manage.link(self, request)

    def unlink(self, relation_id: UUID) -> None:
        self.require_write()
        manage.unlink(self, relation_id)

    def forget(self, memory_id: UUID, mode: ForgetMode) -> ForgetResult:
        self.require_write()
        return manage.forget(self, memory_id, mode)

    def forget_event(self, event_id: UUID) -> ForgetResult:
        self.require_write()
        return manage.forget_event(self, event_id)

    def list_memories(self, filters: ListRequest | None = None) -> MemoryPage:
        return manage.list_memories(self, filters or ListRequest())

    def put_document(self, path: str, request: PutDocumentRequest) -> Document:
        self.require_write(request.scope_drop)
        return documents.put_document(self, path, request)

    def get_document(self, path: str, revision: int | None = None) -> Document:
        return documents.get_document(self, path, revision)

    def list_documents(self) -> list[DocumentInfo]:
        return documents.list_documents(self)

    def list_revisions(self, path: str) -> list[DocumentRevision]:
        return documents.list_revisions(self, path)

    def delete_document(self, path: str) -> None:
        self.require_write()
        documents.delete_document(self, path)

    def export_workspace(self, lesson_ids: list[UUID]) -> WorkspaceSnapshot:
        from memfxxk.workspace import export_workspace

        return export_workspace(self, lesson_ids)

    def import_workspace(self, request: ImportWorkspaceRequest) -> WorkspaceImportResult:
        from memfxxk.workspace import import_workspace

        self.require_write()
        return import_workspace(self, request)

    def get_session(self, session_id: str) -> SessionState:
        return sessions.get_session(self, session_id)

    def put_session_state(self, session_id: str, request: PutSessionState) -> SessionState:
        self.require_write()
        return sessions.put_session_state(self, session_id, request)

    def build_context(self, request: BuildContextRequest) -> ContextBundle:
        return context.build_context(self, request)

    def prepare_compaction(self, request: PrepareCompaction) -> CompactionDraft:
        from memfxxk.m2.compaction import prepare

        self.require_write()
        return prepare(self, request)

    def commit_compaction(self, draft_id: UUID) -> CommittedCompaction:
        from memfxxk.m2.compaction import commit

        self.require_write()
        return commit(self, draft_id)

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
