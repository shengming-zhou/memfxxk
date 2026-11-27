from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from memfxxk.api.dependencies import RequestService
from memfxxk.domain.blocks import (
    BlockEditResult,
    BlockProposal,
    CreateBlock,
    EditBlock,
    MemoryBlock,
    ReviewBlock,
)
from memfxxk.domain.facts import FactProposal, FactReview
from memfxxk.domain.manage import (
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
from memfxxk.domain.retrieval import MemoryResponse, SearchRequest, SearchResponse

router = APIRouter(prefix="/v1")


@router.post("/memory-blocks")
def create_block(request: CreateBlock, service: RequestService) -> MemoryBlock:
    return service.create_block(request)


@router.get("/memory-blocks")
def list_blocks(service: RequestService) -> list[MemoryBlock]:
    return service.list_blocks()


@router.get("/memory-blocks/{block_id}")
def get_block(block_id: UUID, service: RequestService) -> MemoryBlock:
    return service.get_block(block_id)


@router.patch("/memory-blocks/{block_id}")
def edit_block(block_id: UUID, request: EditBlock, service: RequestService) -> BlockEditResult:
    return service.edit_block(block_id, request)


@router.get("/block-proposals")
def list_block_proposals(service: RequestService) -> list[BlockProposal]:
    return service.list_block_proposals()


@router.post("/block-proposals/{proposal_id}/review")
def review_block(
    proposal_id: UUID,
    request: ReviewBlock,
    service: RequestService,
) -> BlockProposal:
    return service.review_block(proposal_id, request.accept)


@router.get("/fact-proposals")
def list_fact_proposals(
    service: RequestService, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[FactProposal]:
    return service.list_fact_proposals(limit)


@router.post("/fact-proposals/{proposal_id}/review")
def review_fact(proposal_id: UUID, request: FactReview, service: RequestService) -> FactProposal:
    return service.review_fact(proposal_id, request.accept)


@router.post("/search", response_model_exclude_none=True)
def search(request: SearchRequest, service: RequestService) -> SearchResponse:
    return service.search(request)


@router.get("/memories/{memory_id}")
def get_memory(
    memory_id: UUID,
    service: RequestService,
    version: Annotated[int | None, Query(ge=1)] = None,
    window: Annotated[int, Query(ge=0)] = 0,
) -> MemoryResponse:
    return service.get_memory(memory_id, version, window)


@router.post("/memories/{memory_id}/note-attributes/rollback")
def rollback_note(
    memory_id: UUID,
    request: RollbackNote,
    service: RequestService,
) -> NoteProjection:
    return service.rollback_note(memory_id, request)


@router.get("/memories/{memory_id}/note-attributes/revisions")
def note_history(
    memory_id: UUID,
    service: RequestService,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[NoteHistoryEntry]:
    return service.note_history(memory_id, limit)


@router.post("/memories")
def remember(request: RememberRequest, service: RequestService) -> MemoryVersion:
    return service.remember(request)


@router.post("/lessons")
def save_lesson(request: SaveLessonRequest, service: RequestService) -> MemoryVersion:
    return service.save_lesson(request)


@router.patch("/memories/{memory_id}")
def patch_memory(memory_id: UUID, request: PatchRequest, service: RequestService) -> MemoryVersion:
    return service.patch_memory(memory_id, request)


@router.get("/memories")
def list_memories(filters: Annotated[ListRequest, Query()], service: RequestService) -> MemoryPage:
    return service.list_memories(filters)


@router.post("/memories/{memory_id}/forget")
def forget(memory_id: UUID, request: ForgetRequest, service: RequestService) -> ForgetResult:
    return service.forget(memory_id, request.mode)


@router.post("/events/{event_id}/forget")
def forget_event(event_id: UUID, service: RequestService) -> ForgetResult:
    return service.forget_event(event_id)


@router.post("/relations")
def link(request: LinkRequest, service: RequestService) -> LinkResult:
    return service.link(request)


@router.delete("/relations/{relation_id}", status_code=204)
def unlink(relation_id: UUID, service: RequestService) -> Response:
    service.unlink(relation_id)
    return Response(status_code=204)
