from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from types import NoneType, TracebackType
from typing import Self
from uuid import UUID

import httpx
from pydantic import BaseModel

from memfxxk.client._shared import (
    ClientContext,
    ClientError,
    Conflict,
    NotFound,
    PermissionDenied,
    ServerError,
    ValidationError,
    base_address,
    context_request,
    decode,
    document_endpoint,
    segment,
    turn_events,
)
from memfxxk.domain import Actor
from memfxxk.domain.blocks import (
    BlockEditResult,
    BlockProposal,
    CreateBlock,
    EditBlock,
    MemoryBlock,
    ReviewBlock,
)
from memfxxk.domain.consistency import (
    ConsistencyRequest,
    ConsistencyResult,
    OutlineRequest,
    OutlineResult,
)
from memfxxk.domain.context import (
    BuildContextRequest,
    BundleMetadata,
    CommitCompaction,
    CommittedCompaction,
    CompactionDraft,
    ContextBundle,
    PrepareCompaction,
)
from memfxxk.domain.documents import Document, DocumentInfo, DocumentRevision, PutDocumentRequest
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.domain.facts import FactProposal, FactReview
from memfxxk.domain.feedback import AllocationChange, AllocationState, FeedbackReport
from memfxxk.domain.lessons import FinishedTask, FinishTask, LessonProposal, ReviewLesson
from memfxxk.domain.manage import (
    ForgetMode,
    ForgetRequest,
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
from memfxxk.domain.priorities import ClearPriority, PriorityResult, PriorityTarget, SetPriority
from memfxxk.domain.reflect import ReflectRequest, ReflectResult
from memfxxk.domain.resolutions import ContentResolutions, ResolutionTarget
from memfxxk.domain.retrieval import MemoryResponse, SearchRequest, SearchResponse
from memfxxk.domain.sessions import PutSessionState, SessionState
from memfxxk.domain.story import StoryAnnotation
from memfxxk.domain.workspace import (
    ImportWorkspaceRequest,
    WorkspaceImportResult,
    WorkspaceSnapshot,
)
from memfxxk.tools import AsyncHttpBackend, HttpBackend, dispatch, dispatch_async, to_openai_tools

__all__ = [
    "AsyncMemfxxkClient",
    "ClientError",
    "Conflict",
    "MemfxxkClient",
    "NotFound",
    "PermissionDenied",
    "ServerError",
    "ValidationError",
]


def _request_data(
    body: BaseModel | dict[str, object] | None, params: dict[str, object] | None
) -> tuple[dict[str, object] | None, dict[str, object]]:
    payload = (
        body.model_dump(mode="json", exclude_unset=True) if isinstance(body, BaseModel) else body
    )
    query = {key: value for key, value in (params or {}).items() if value is not None}
    return payload, query


class MemfxxkClient(ClientContext):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
        actor: Actor | dict[str, object] | None = None,
        timeout: float | httpx.Timeout = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(api_key, user_id, agent_id, project_id, actor)
        self._http = httpx.Client(
            base_url=base_address(base_url), timeout=timeout, transport=transport
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self._http.close()

    def _request[T](
        self,
        method: str,
        path: str,
        result: type[T],
        *,
        body: BaseModel | dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> T:
        payload, query = _request_data(body, params)
        try:
            response = self._http.request(
                method, path, json=payload, params=query, headers=self._headers()
            )
        except httpx.RequestError as exc:
            raise ServerError(503, "connection_error", "Could not connect to memfxxk") from exc
        return decode(response, result)

    def healthz(self) -> dict[str, str]:
        return self._request("GET", "healthz", dict[str, str])

    def ingest(self, events: list[IngestEvent]) -> list[IngestResult]:
        return self._request(
            "POST",
            "events",
            list[IngestResult],
            body={
                "events": [event.model_dump(mode="json", exclude_unset=True) for event in events]
            },
        )

    def get_event(self, event_id: UUID | str) -> Event:
        return self._request("GET", f"events/{segment(event_id)}", Event)

    def forget_event(self, event_id: UUID | str) -> ForgetResult:
        return self._request("POST", f"events/{segment(event_id)}/forget", ForgetResult)

    def list_session_events(
        self,
        session_id: str,
        from_seq: int | None = None,
        to_seq: int | None = None,
        limit: int = 100,
    ) -> list[Event]:
        return self._request(
            "GET",
            f"sessions/{segment(session_id)}/events",
            list[Event],
            params={"from_seq": from_seq, "to_seq": to_seq, "limit": limit},
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._request("POST", "search", SearchResponse, body=request)

    def get_memory(
        self, memory_id: UUID | str, version: int | None = None, window: int = 0
    ) -> MemoryResponse:
        return self._request(
            "GET",
            f"memories/{segment(memory_id)}",
            MemoryResponse,
            params={"version": version, "window": window},
        )

    def remember(self, request: RememberRequest) -> MemoryVersion:
        return self._request("POST", "memories", MemoryVersion, body=request)

    def create_block(self, request: CreateBlock) -> MemoryBlock:
        return self._request("POST", "memory-blocks", MemoryBlock, body=request)

    def list_blocks(self) -> list[MemoryBlock]:
        return self._request("GET", "memory-blocks", list[MemoryBlock])

    def get_block(self, block_id: UUID | str) -> MemoryBlock:
        return self._request("GET", f"memory-blocks/{segment(block_id)}", MemoryBlock)

    def edit_block(self, block_id: UUID | str, request: EditBlock) -> BlockEditResult:
        return self._request(
            "PATCH", f"memory-blocks/{segment(block_id)}", BlockEditResult, body=request
        )

    def list_block_proposals(self) -> list[BlockProposal]:
        return self._request("GET", "block-proposals", list[BlockProposal])

    def review_block(self, proposal_id: UUID | str, accept: bool) -> BlockProposal:
        return self._request(
            "POST",
            f"block-proposals/{segment(proposal_id)}/review",
            BlockProposal,
            body=ReviewBlock(accept=accept),
        )

    def rollback_note(self, memory_id: UUID | str, request: RollbackNote) -> NoteProjection:
        return self._request(
            "POST",
            f"memories/{segment(memory_id)}/note-attributes/rollback",
            NoteProjection,
            body=request,
        )

    def note_history(self, memory_id: UUID | str, limit: int = 50) -> list[NoteHistoryEntry]:
        return self._request(
            "GET",
            f"memories/{segment(memory_id)}/note-attributes/revisions",
            list[NoteHistoryEntry],
            params={"limit": limit},
        )

    def save_lesson(self, request: SaveLessonRequest) -> MemoryVersion:
        return self._request("POST", "lessons", MemoryVersion, body=request)

    def patch_memory(self, memory_id: UUID | str, request: PatchRequest) -> MemoryVersion:
        return self._request("PATCH", f"memories/{segment(memory_id)}", MemoryVersion, body=request)

    def list_memories(self, filters: ListRequest | None = None) -> MemoryPage:
        return self._request(
            "GET",
            "memories",
            MemoryPage,
            params=filters.model_dump(mode="json", exclude_none=True, exclude_unset=True)
            if filters
            else None,
        )

    def forget(self, memory_id: UUID | str, mode: ForgetMode = "deactivate") -> ForgetResult:
        return self._request(
            "POST",
            f"memories/{segment(memory_id)}/forget",
            ForgetResult,
            body=ForgetRequest(mode=mode),
        )

    forget_memory = forget

    def link(self, request: LinkRequest) -> LinkResult:
        return self._request("POST", "relations", LinkResult, body=request)

    def unlink(self, relation_id: UUID | str) -> None:
        return self._request("DELETE", f"relations/{segment(relation_id)}", NoneType)

    def put_document(self, path: str, request: PutDocumentRequest) -> Document:
        return self._request("PUT", document_endpoint(path), Document, body=request)

    def get_document(self, path: str, revision: int | None = None) -> Document:
        return self._request("GET", document_endpoint(path, revision), Document)

    def list_documents(self) -> list[DocumentInfo]:
        return self._request("GET", "documents", list[DocumentInfo])

    def list_revisions(self, path: str) -> list[DocumentRevision]:
        return self._request("GET", f"{document_endpoint(path)}/revisions", list[DocumentRevision])

    def delete_document(self, path: str) -> None:
        return self._request("DELETE", document_endpoint(path), NoneType)

    def export_workspace(self, lesson_ids: Iterable[UUID] = ()) -> WorkspaceSnapshot:
        return self._request(
            "GET",
            "workspace",
            WorkspaceSnapshot,
            params={
                "lesson_ids": [str(lesson_id) for lesson_id in lesson_ids],
            },
        )

    def import_workspace(self, request: ImportWorkspaceRequest) -> WorkspaceImportResult:
        return self._request("POST", "workspace", WorkspaceImportResult, body=request)

    def get_session(self, session_id: str) -> SessionState:
        return self._request("GET", f"sessions/{segment(session_id)}/state", SessionState)

    def get_priority(self, target: PriorityTarget) -> PriorityResult:
        return self._request("POST", "priorities/resolve", PriorityResult, body=target)

    def get_resolutions(self, target: ResolutionTarget) -> ContentResolutions:
        return self._request("POST", "context/resolutions", ContentResolutions, body=target)

    def set_priority(self, request: SetPriority) -> PriorityResult:
        return self._request("PUT", "priorities", PriorityResult, body=request)

    def clear_priority(self, request: ClearPriority) -> PriorityResult:
        return self._request("POST", "priorities/clear", PriorityResult, body=request)

    def finish_task(self, session_id: str, request: FinishTask) -> FinishedTask:
        return self._request(
            "POST", f"sessions/{segment(session_id)}/finish-task", FinishedTask, body=request
        )

    def list_lesson_proposals(self, limit: int = 50) -> list[LessonProposal]:
        return self._request(
            "GET", "lesson-proposals", list[LessonProposal], params={"limit": limit}
        )

    def review_lesson(self, memory_id: UUID | str, request: ReviewLesson) -> LessonProposal:
        return self._request(
            "POST", f"lesson-proposals/{segment(memory_id)}/review", LessonProposal, body=request
        )

    def put_session_state(self, session_id: str, request: PutSessionState) -> SessionState:
        return self._request(
            "PUT", f"sessions/{segment(session_id)}/state", SessionState, body=request
        )

    def get_bundle(self, bundle_id: UUID | str) -> BundleMetadata:
        return self._request("GET", f"context/bundles/{segment(bundle_id)}", BundleMetadata)

    def reflect(self, request: ReflectRequest) -> ReflectResult:
        return self._request("POST", "reflect", ReflectResult, body=request)

    def get_allocation_state(self) -> AllocationState:
        return self._request("GET", "context/allocation", AllocationState)

    def get_allocation_history(self, after: int = 0, limit: int = 50) -> list[AllocationChange]:
        return self._request(
            "GET",
            "context/allocation/history",
            list[AllocationChange],
            params={"after": after, "limit": limit},
        )

    def reset_allocation_state(self) -> AllocationState:
        return self._request("POST", "context/allocation/reset", AllocationState)

    def report_feedback(self, request: FeedbackReport) -> AllocationState:
        return self._request("POST", "context/allocation/feedback", AllocationState, body=request)

    def check_consistency(self, request: ConsistencyRequest) -> ConsistencyResult:
        return self._request("POST", "context/consistency", ConsistencyResult, body=request)

    def get_outline(self, request: OutlineRequest) -> OutlineResult:
        return self._request("POST", "context/outline", OutlineResult, body=request)

    def annotate_story_time(self, request: StoryAnnotation) -> StoryAnnotation:
        return self._request("PUT", "context/story-time", StoryAnnotation, body=request)

    def build_context(
        self,
        session_id: str | BuildContextRequest | None = None,
        query: str | None = None,
        **budget: int,
    ) -> ContextBundle:
        return self._request(
            "POST", "context/build", ContextBundle, body=context_request(session_id, query, budget)
        )

    def ingest_turn(
        self,
        session_id: str,
        user: str,
        assistant: str,
        tool_events: Iterable[IngestEvent] = (),
        turn_id: str | None = None,
        event_time: datetime | None = None,
    ) -> list[IngestResult]:
        return self.ingest(
            turn_events(session_id, user, assistant, tool_events, turn_id, event_time)
        )

    def prepare_compaction(self, request: PrepareCompaction) -> CompactionDraft:
        return self._request("POST", "context/compactions", CompactionDraft, body=request)

    def list_fact_proposals(self, limit: int = 50) -> list[FactProposal]:
        return self._request("GET", "fact-proposals", list[FactProposal], params={"limit": limit})

    def review_fact(self, proposal_id: UUID | str, *, accept: bool) -> FactProposal:
        return self._request(
            "POST",
            f"fact-proposals/{segment(proposal_id)}/review",
            FactProposal,
            body=FactReview(accept=accept),
        )

    def commit_compaction(self, draft_id: UUID) -> CommittedCompaction:
        return self._request(
            "POST",
            "context/compactions/commit",
            CommittedCompaction,
            body=CommitCompaction(draft_id=draft_id),
        )

    def tools(
        self,
    ) -> tuple[list[dict[str, object]], Callable[[str, dict[str, object]], str]]:
        backend = HttpBackend(self)

        def handler(name: str, arguments: dict[str, object]) -> str:
            return dispatch(backend, name, arguments)

        return to_openai_tools(), handler


class AsyncMemfxxkClient(ClientContext):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
        actor: Actor | dict[str, object] | None = None,
        timeout: float | httpx.Timeout = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(api_key, user_id, agent_id, project_id, actor)
        self._http = httpx.AsyncClient(
            base_url=base_address(base_url), timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_connection:
            await self._http.aclose()

    async def _request[T](
        self,
        method: str,
        path: str,
        result: type[T],
        *,
        body: BaseModel | dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> T:
        payload, query = _request_data(body, params)
        try:
            response = await self._http.request(
                method, path, json=payload, params=query, headers=self._headers()
            )
        except httpx.RequestError as exc:
            raise ServerError(503, "connection_error", "Could not connect to memfxxk") from exc
        return decode(response, result)

    async def healthz(self) -> dict[str, str]:
        return await self._request("GET", "healthz", dict[str, str])

    async def ingest(self, events: list[IngestEvent]) -> list[IngestResult]:
        return await self._request(
            "POST",
            "events",
            list[IngestResult],
            body={
                "events": [event.model_dump(mode="json", exclude_unset=True) for event in events]
            },
        )

    async def get_event(self, event_id: UUID | str) -> Event:
        return await self._request("GET", f"events/{segment(event_id)}", Event)

    async def forget_event(self, event_id: UUID | str) -> ForgetResult:
        return await self._request("POST", f"events/{segment(event_id)}/forget", ForgetResult)

    async def list_session_events(
        self,
        session_id: str,
        from_seq: int | None = None,
        to_seq: int | None = None,
        limit: int = 100,
    ) -> list[Event]:
        return await self._request(
            "GET",
            f"sessions/{segment(session_id)}/events",
            list[Event],
            params={"from_seq": from_seq, "to_seq": to_seq, "limit": limit},
        )

    async def search(self, request: SearchRequest) -> SearchResponse:
        return await self._request("POST", "search", SearchResponse, body=request)

    async def get_memory(
        self, memory_id: UUID | str, version: int | None = None, window: int = 0
    ) -> MemoryResponse:
        return await self._request(
            "GET",
            f"memories/{segment(memory_id)}",
            MemoryResponse,
            params={"version": version, "window": window},
        )

    async def remember(self, request: RememberRequest) -> MemoryVersion:
        return await self._request("POST", "memories", MemoryVersion, body=request)

    async def create_block(self, request: CreateBlock) -> MemoryBlock:
        return await self._request("POST", "memory-blocks", MemoryBlock, body=request)

    async def list_blocks(self) -> list[MemoryBlock]:
        return await self._request("GET", "memory-blocks", list[MemoryBlock])

    async def get_block(self, block_id: UUID | str) -> MemoryBlock:
        return await self._request("GET", f"memory-blocks/{segment(block_id)}", MemoryBlock)

    async def edit_block(self, block_id: UUID | str, request: EditBlock) -> BlockEditResult:
        return await self._request(
            "PATCH", f"memory-blocks/{segment(block_id)}", BlockEditResult, body=request
        )

    async def list_block_proposals(self) -> list[BlockProposal]:
        return await self._request("GET", "block-proposals", list[BlockProposal])

    async def review_block(self, proposal_id: UUID | str, accept: bool) -> BlockProposal:
        return await self._request(
            "POST",
            f"block-proposals/{segment(proposal_id)}/review",
            BlockProposal,
            body=ReviewBlock(accept=accept),
        )

    async def rollback_note(self, memory_id: UUID | str, request: RollbackNote) -> NoteProjection:
        return await self._request(
            "POST",
            f"memories/{segment(memory_id)}/note-attributes/rollback",
            NoteProjection,
            body=request,
        )

    async def note_history(self, memory_id: UUID | str, limit: int = 50) -> list[NoteHistoryEntry]:
        return await self._request(
            "GET",
            f"memories/{segment(memory_id)}/note-attributes/revisions",
            list[NoteHistoryEntry],
            params={"limit": limit},
        )

    async def save_lesson(self, request: SaveLessonRequest) -> MemoryVersion:
        return await self._request("POST", "lessons", MemoryVersion, body=request)

    async def patch_memory(self, memory_id: UUID | str, request: PatchRequest) -> MemoryVersion:
        return await self._request(
            "PATCH", f"memories/{segment(memory_id)}", MemoryVersion, body=request
        )

    async def list_memories(self, filters: ListRequest | None = None) -> MemoryPage:
        return await self._request(
            "GET",
            "memories",
            MemoryPage,
            params=filters.model_dump(mode="json", exclude_none=True, exclude_unset=True)
            if filters
            else None,
        )

    async def forget(self, memory_id: UUID | str, mode: ForgetMode = "deactivate") -> ForgetResult:
        return await self._request(
            "POST",
            f"memories/{segment(memory_id)}/forget",
            ForgetResult,
            body=ForgetRequest(mode=mode),
        )

    forget_memory = forget

    async def link(self, request: LinkRequest) -> LinkResult:
        return await self._request("POST", "relations", LinkResult, body=request)

    async def unlink(self, relation_id: UUID | str) -> None:
        return await self._request("DELETE", f"relations/{segment(relation_id)}", NoneType)

    async def put_document(self, path: str, request: PutDocumentRequest) -> Document:
        return await self._request("PUT", document_endpoint(path), Document, body=request)

    async def get_document(self, path: str, revision: int | None = None) -> Document:
        return await self._request("GET", document_endpoint(path, revision), Document)

    async def list_documents(self) -> list[DocumentInfo]:
        return await self._request("GET", "documents", list[DocumentInfo])

    async def list_revisions(self, path: str) -> list[DocumentRevision]:
        return await self._request(
            "GET", f"{document_endpoint(path)}/revisions", list[DocumentRevision]
        )

    async def delete_document(self, path: str) -> None:
        return await self._request("DELETE", document_endpoint(path), NoneType)

    async def export_workspace(self, lesson_ids: Iterable[UUID] = ()) -> WorkspaceSnapshot:
        return await self._request(
            "GET",
            "workspace",
            WorkspaceSnapshot,
            params={
                "lesson_ids": [str(lesson_id) for lesson_id in lesson_ids],
            },
        )

    async def import_workspace(self, request: ImportWorkspaceRequest) -> WorkspaceImportResult:
        return await self._request("POST", "workspace", WorkspaceImportResult, body=request)

    async def get_session(self, session_id: str) -> SessionState:
        return await self._request("GET", f"sessions/{segment(session_id)}/state", SessionState)

    async def get_priority(self, target: PriorityTarget) -> PriorityResult:
        return await self._request("POST", "priorities/resolve", PriorityResult, body=target)

    async def get_resolutions(self, target: ResolutionTarget) -> ContentResolutions:
        return await self._request("POST", "context/resolutions", ContentResolutions, body=target)

    async def set_priority(self, request: SetPriority) -> PriorityResult:
        return await self._request("PUT", "priorities", PriorityResult, body=request)

    async def clear_priority(self, request: ClearPriority) -> PriorityResult:
        return await self._request("POST", "priorities/clear", PriorityResult, body=request)

    async def finish_task(self, session_id: str, request: FinishTask) -> FinishedTask:
        return await self._request(
            "POST", f"sessions/{segment(session_id)}/finish-task", FinishedTask, body=request
        )

    async def list_lesson_proposals(self, limit: int = 50) -> list[LessonProposal]:
        return await self._request(
            "GET", "lesson-proposals", list[LessonProposal], params={"limit": limit}
        )

    async def review_lesson(self, memory_id: UUID | str, request: ReviewLesson) -> LessonProposal:
        return await self._request(
            "POST", f"lesson-proposals/{segment(memory_id)}/review", LessonProposal, body=request
        )

    async def put_session_state(self, session_id: str, request: PutSessionState) -> SessionState:
        return await self._request(
            "PUT", f"sessions/{segment(session_id)}/state", SessionState, body=request
        )

    async def get_bundle(self, bundle_id: UUID | str) -> BundleMetadata:
        return await self._request("GET", f"context/bundles/{segment(bundle_id)}", BundleMetadata)

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        return await self._request("POST", "reflect", ReflectResult, body=request)

    async def get_allocation_state(self) -> AllocationState:
        return await self._request("GET", "context/allocation", AllocationState)

    async def get_allocation_history(
        self, after: int = 0, limit: int = 50
    ) -> list[AllocationChange]:
        return await self._request(
            "GET",
            "context/allocation/history",
            list[AllocationChange],
            params={"after": after, "limit": limit},
        )

    async def reset_allocation_state(self) -> AllocationState:
        return await self._request("POST", "context/allocation/reset", AllocationState)

    async def report_feedback(self, request: FeedbackReport) -> AllocationState:
        return await self._request(
            "POST", "context/allocation/feedback", AllocationState, body=request
        )

    async def check_consistency(self, request: ConsistencyRequest) -> ConsistencyResult:
        return await self._request("POST", "context/consistency", ConsistencyResult, body=request)

    async def get_outline(self, request: OutlineRequest) -> OutlineResult:
        return await self._request("POST", "context/outline", OutlineResult, body=request)

    async def annotate_story_time(self, request: StoryAnnotation) -> StoryAnnotation:
        return await self._request("PUT", "context/story-time", StoryAnnotation, body=request)

    async def build_context(
        self,
        session_id: str | BuildContextRequest | None = None,
        query: str | None = None,
        **budget: int,
    ) -> ContextBundle:
        return await self._request(
            "POST", "context/build", ContextBundle, body=context_request(session_id, query, budget)
        )

    async def ingest_turn(
        self,
        session_id: str,
        user: str,
        assistant: str,
        tool_events: Iterable[IngestEvent] = (),
        turn_id: str | None = None,
        event_time: datetime | None = None,
    ) -> list[IngestResult]:
        return await self.ingest(
            turn_events(session_id, user, assistant, tool_events, turn_id, event_time)
        )

    async def prepare_compaction(self, request: PrepareCompaction) -> CompactionDraft:
        return await self._request("POST", "context/compactions", CompactionDraft, body=request)

    async def list_fact_proposals(self, limit: int = 50) -> list[FactProposal]:
        return await self._request(
            "GET", "fact-proposals", list[FactProposal], params={"limit": limit}
        )

    async def review_fact(self, proposal_id: UUID | str, *, accept: bool) -> FactProposal:
        return await self._request(
            "POST",
            f"fact-proposals/{segment(proposal_id)}/review",
            FactProposal,
            body=FactReview(accept=accept),
        )

    async def commit_compaction(self, draft_id: UUID) -> CommittedCompaction:
        return await self._request(
            "POST",
            "context/compactions/commit",
            CommittedCompaction,
            body=CommitCompaction(draft_id=draft_id),
        )

    def tools(
        self,
    ) -> tuple[list[dict[str, object]], Callable[[str, dict[str, object]], Awaitable[str]]]:
        backend = AsyncHttpBackend(self)

        async def handler(name: str, arguments: dict[str, object]) -> str:
            return await dispatch_async(backend, name, arguments)

        return to_openai_tools(), handler
