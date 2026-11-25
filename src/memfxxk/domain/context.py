from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from memfxxk.domain.retrieval import MemoryType, SearchChannel, SearchTimings

SectionName = Literal["documents", "pinned", "working", "evidence"]
SectionKind = Literal["document", "index", "memory", "event", "summary"]


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: list[MemoryType] | None = None
    time_from: AwareDatetime | None = None
    time_to: AwareDatetime | None = None
    session_id: str | None = None
    channels: list[SearchChannel] | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if (
            self.time_from is not None
            and self.time_to is not None
            and self.time_from > self.time_to
        ):
            raise ValueError("time_from must be before or equal to time_to")
        return self


class BuildContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    query: str | None = None
    budget: dict[SectionName, Annotated[int, Field(ge=0)]] | None = None
    auto_recall: bool = True
    k: int = Field(default=8, ge=1, le=100)
    search_filters: SearchFilters = Field(default_factory=SearchFilters)
    include_on_demand_index: bool = True
    sections: list[SectionName] = Field(
        default_factory=lambda: ["documents", "pinned", "working", "evidence"]
    )


class ContextReference(BaseModel):
    kind: SectionKind
    ref_id: str
    version: int | None = None
    revision: int | None = None
    tokens: int = 0
    truncated: bool = False
    reason: str
    score: float | None = None
    related: list[UUID] = Field(default_factory=list)


class ContextSection(BaseModel):
    name: SectionName
    budget: int
    used_tokens: int
    items: list[ContextReference]
    dropped: list[ContextReference]
    rendered: str = ""


class ContextBundle(BaseModel):
    bundle_id: UUID
    sections: list[ContextSection]
    rendered: str
    total_tokens: int
    retrieval_timings_ms: SearchTimings | None = None


class BundleMetadata(BaseModel):
    bundle_id: UUID
    sections: list[ContextSection]
    total_tokens: int
    created_at: AwareDatetime
