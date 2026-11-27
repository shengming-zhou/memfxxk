from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field


class FactCandidate(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=4000)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=50)
    valid_from: AwareDatetime | None = None
    entities: list[str] = Field(default_factory=list, max_length=8)


class FactProposal(BaseModel):
    id: UUID
    fact: FactCandidate
    status: Literal["pending", "accepted", "rejected"]
    created_at: AwareDatetime
    memory_id: UUID | None = None
    memory_version: int | None = None
    operation: Literal["propose", "update", "delete"] = "propose"
    base_version: int | None = None


class FactReview(BaseModel):
    accept: bool
