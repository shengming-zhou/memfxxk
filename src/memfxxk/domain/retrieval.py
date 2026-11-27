from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from memfxxk.domain import Actor, Scope

MemoryType = Literal["turn", "note", "fact", "summary", "lesson", "doc_chunk"]
MemoryMark = Literal["important", "pinned"]
SearchChannel = Literal["lexical", "vector", "important"]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    k: int = Field(default=10, gt=0)
    types: list[MemoryType] | None = None
    time_from: AwareDatetime | None = None
    time_to: AwareDatetime | None = None
    session_id: str | None = None
    include_inactive: bool = False
    expand_relations: bool = True
    graph_hops: int = Field(default=0, ge=0, le=2)
    tags: list[str] = Field(default_factory=list, max_length=32)
    as_of_valid: AwareDatetime | None = None
    as_of_recorded: AwareDatetime | None = None
    channels: list[SearchChannel] = Field(
        default_factory=lambda: ["lexical", "vector", "important"]
    )

    @model_validator(mode="after")
    def validate_time_range(self) -> Self:
        if (
            self.time_from is not None
            and self.time_to is not None
            and self.time_from > self.time_to
        ):
            raise ValueError("time_from must be before or equal to time_to")
        return self


class ScoreDetails(BaseModel):
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: float | None = None
    vector_sim: float | None = None
    important: bool = False
    rrf: float
    boost: float
    final: float
    fact_bm25: float | None = None
    fact_entity_boost: float | None = None


class RelatedMemory(BaseModel):
    memory_id: UUID
    purpose: str
    direction: Literal["outgoing", "incoming"]
    type: MemoryType
    snippet: str
    event_time: AwareDatetime


class MemoryReference(BaseModel):
    memory_id: UUID
    version: int
    type: MemoryType
    title: str | None = None
    event_time: AwareDatetime
    recorded_at: AwareDatetime
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    scope: Scope
    session_id: str | None = None
    source_pos: dict[str, object] | None = None


class SearchHit(MemoryReference):
    snippet: str
    content_truncated: bool
    marks: list[MemoryMark]
    related: list[RelatedMemory] = Field(default_factory=list)
    graph_path: list[UUID] = Field(default_factory=list)
    priority: int | None = None
    tags: list[str] = Field(default_factory=list)
    score: ScoreDetails


class SearchTimings(BaseModel):
    embed: float = 0
    lexical: float = 0
    vector: float = 0
    important: float = 0
    fuse: float = 0
    expand: float = 0
    graph: float = 0
    total: float = 0


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    query_tokens: list[str]
    timings_ms: SearchTimings


class MemoryVersion(BaseModel):
    version: int
    recorded_at: AwareDatetime
    created_by: Actor


class MemoryNeighbor(MemoryReference):
    content: str


class MemoryNeighbors(BaseModel):
    before: list[MemoryNeighbor] = Field(default_factory=list)
    after: list[MemoryNeighbor] = Field(default_factory=list)


class MemoryResponse(MemoryNeighbor):
    origin: Literal["user_explicit", "auto_index", "auto_extract", "import"]
    status: Literal["active", "inactive"]
    confirmed: bool
    source_event_ids: list[UUID]
    marks: list[MemoryMark]
    relations: list[RelatedMemory]
    versions: list[MemoryVersion]
    neighbors: MemoryNeighbors
    priority: int | None = None
    tags: list[str] = Field(default_factory=list)
