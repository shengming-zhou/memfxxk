from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from memfxxk.api.dependencies import RequestService
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
from memfxxk.domain.feedback import AllocationChange, AllocationState, FeedbackReport
from memfxxk.domain.lessons import FinishedTask, FinishTask
from memfxxk.domain.priorities import ClearPriority, PriorityResult, PriorityTarget, SetPriority
from memfxxk.domain.reflect import ReflectRequest, ReflectResult
from memfxxk.domain.resolutions import ContentResolutions, ResolutionTarget
from memfxxk.domain.sessions import PutSessionState, SessionState
from memfxxk.domain.story import StoryAnnotation
from memfxxk.domain.workspace import (
    ImportWorkspaceRequest,
    WorkspaceImportResult,
    WorkspaceSnapshot,
)

router = APIRouter(prefix="/v1")


@router.get("/workspace")
def export_workspace(
    service: RequestService,
    lesson_ids: Annotated[list[UUID] | None, Query()] = None,
) -> WorkspaceSnapshot:
    return service.export_workspace(lesson_ids or [])


@router.post("/workspace")
def import_workspace(
    request: ImportWorkspaceRequest, service: RequestService
) -> WorkspaceImportResult:
    return service.import_workspace(request)


@router.get("/documents")
def list_documents(service: RequestService) -> list[DocumentInfo]:
    return service.list_documents()


@router.get("/documents/{path:path}/revisions/{revision:int}")
def read_document_revision(path: str, revision: int, service: RequestService) -> Document:
    return service.get_document(path, revision)


@router.get("/documents/{path:path}/revisions")
def list_revisions(path: str, service: RequestService) -> list[DocumentRevision]:
    return service.list_revisions(path)


@router.get("/documents/{path:path}")
def read_document(path: str, service: RequestService) -> Document:
    return service.get_document(path)


@router.put("/documents/{path:path}")
def put_document(path: str, request: PutDocumentRequest, service: RequestService) -> Document:
    return service.put_document(path, request)


@router.delete("/documents/{path:path}", status_code=204)
def delete_document(path: str, service: RequestService) -> Response:
    service.delete_document(path)
    return Response(status_code=204)


@router.get("/sessions/{session_id}/state")
def get_state(session_id: str, service: RequestService) -> SessionState:
    return service.get_session(session_id)


@router.put("/sessions/{session_id}/state")
def put_state(session_id: str, request: PutSessionState, service: RequestService) -> SessionState:
    return service.put_session_state(session_id, request)


@router.post("/context/build")
def build_context(request: BuildContextRequest, service: RequestService) -> ContextBundle:
    return service.build_context(request)


@router.post("/context/consistency")
def check_consistency(request: ConsistencyRequest, service: RequestService) -> ConsistencyResult:
    return service.check_consistency(request)


@router.post("/context/outline")
def get_outline(request: OutlineRequest, service: RequestService) -> OutlineResult:
    return service.get_outline(request)


@router.put("/context/story-time")
def annotate_story_time(request: StoryAnnotation, service: RequestService) -> StoryAnnotation:
    return service.annotate_story_time(request)


@router.get("/context/allocation")
def allocation_state(service: RequestService) -> AllocationState:
    return service.get_allocation_state()


@router.get("/context/allocation/history")
def allocation_history(
    service: RequestService,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AllocationChange]:
    return service.get_allocation_history(after, limit)


@router.post("/context/allocation/reset")
def reset_allocation_state(service: RequestService) -> AllocationState:
    return service.reset_allocation_state()


@router.post("/context/allocation/feedback")
def report_feedback(request: FeedbackReport, service: RequestService) -> AllocationState:
    return service.report_feedback(request)


@router.post("/priorities/resolve")
def get_priority(target: PriorityTarget, service: RequestService) -> PriorityResult:
    return service.get_priority(target)


@router.post("/context/resolutions")
def read_resolutions(target: ResolutionTarget, service: RequestService) -> ContentResolutions:
    return service.get_resolutions(target)


@router.put("/priorities")
def set_priority(request: SetPriority, service: RequestService) -> PriorityResult:
    return service.set_priority(request)


@router.post("/priorities/clear")
def clear_priority(request: ClearPriority, service: RequestService) -> PriorityResult:
    return service.clear_priority(request)


@router.post("/sessions/{session_id}/finish-task")
def finish_task(session_id: str, request: FinishTask, service: RequestService) -> FinishedTask:
    return service.finish_task(session_id, request)


@router.post("/reflect")
def reflect(request: ReflectRequest, service: RequestService) -> ReflectResult:
    return service.reflect(request)


@router.post("/context/compactions")
def prepare_compaction(request: PrepareCompaction, service: RequestService) -> CompactionDraft:
    return service.prepare_compaction(request)


@router.post("/context/compactions/commit")
def commit_compaction(request: CommitCompaction, service: RequestService) -> CommittedCompaction:
    return service.commit_compaction(request.draft_id)


@router.get("/context/bundles/{bundle_id}", response_model_exclude_defaults=True)
def get_bundle(bundle_id: UUID, service: RequestService) -> BundleMetadata:
    return service.get_bundle(bundle_id)
