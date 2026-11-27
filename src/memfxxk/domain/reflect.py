from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Disposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skepticism: int = Field(default=3, ge=1, le=5)
    literalness: int = Field(default=3, ge=1, le=5)
    empathy: int = Field(default=3, ge=1, le=5)
    mission: str = Field(default="", max_length=2000)


class ReflectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=12_000)
    max_tokens: int = Field(default=1500, ge=1, le=32000)
    max_rounds: int = Field(default=6, ge=1, le=10)
    query_time: AwareDatetime | None = None
    store_opinions: bool = False
    disposition: Disposition = Field(default_factory=Disposition)


class ReflectCitation(BaseModel):
    id: UUID
    kind: Literal["memory", "block"]
    record_type: str
    version: int


class OpinionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    citations: list[UUID] = Field(min_length=1, max_length=20)


class OpinionChange(BaseModel):
    memory_id: UUID
    version: int
    confidence: float
    reason: Literal["created", "strengthens", "weakens", "contradicts"]


class ReflectResult(BaseModel):
    answer: str
    citations: list[ReflectCitation]
    rejected_citations: list[UUID]
    proposals: list[OpinionSuggestion]
    opinion_changes: list[OpinionChange]
    bundle_ids: list[UUID]
    rounds: int
