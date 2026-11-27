from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from memfxxk.api.dependencies import RequestService
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
from memfxxk.domain.reflect import ReflectRequest, ReflectResult
from memfxxk.domain.sessions import PutSessionState, SessionState
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
