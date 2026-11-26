from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from memfxxk.api.dependencies import RequestService
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
from memfxxk.domain.retrieval import MemoryResponse, SearchRequest, SearchResponse

router = APIRouter(prefix="/v1")


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
