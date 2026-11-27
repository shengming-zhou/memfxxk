from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from memfxxk.domain.retrieval import MemoryType, SearchChannel, SearchTimings
from memfxxk.prompts.compact import Checkpoint

SectionName = Literal["documents", "pinned", "working", "evidence"]
CompactionMethod = Literal["legacy", "llm", "extractive_fallback"]
Pressure = Literal["normal", "warning"]
SectionKind = Literal["document", "index", "memory", "event", "summary", "block"]


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: list[MemoryType] | None = None
    time_from: AwareDatetime | None = None
    time_to: AwareDatetime | None = None
    session_id: str | None = None
    channels: list[SearchChannel] | None = None
    graph_hops: int = Field(default=0, ge=0, le=2)
    tags: list[str] = Field(default_factory=list, max_length=32)
    as_of_valid: AwareDatetime | None = None
    as_of_recorded: AwareDatetime | None = None

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
    max_tokens: int | None = Field(default=None, ge=0)
    graph_evidence_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Reserve up to this many evidence tokens for graph hits when graph_hops is enabled."
        ),
    )
    host_history_from_seq: int | None = Field(default=None, gt=0)
    evidence_mode: Literal["full", "excerpt"] = "full"
    auto_recall: bool = True
    k: int = Field(default=8, ge=1, le=100)
    search_filters: SearchFilters = Field(default_factory=SearchFilters)
    include_on_demand_index: bool = True
    sections: list[SectionName] = Field(
        default_factory=lambda: ["documents", "pinned", "working", "evidence"]
    )


class PrepareCompaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    through_seq: int = Field(gt=0)
    target_tokens: int = Field(gt=0, le=16_384)


class CompactionDraft(BaseModel):
    draft_id: UUID
    session_id: str
    from_seq: int
    through_seq: int
    base_revision: int
    summary: str
    tokens: int
    source_event_ids: list[UUID]
    method: CompactionMethod = "legacy"
    checkpoint: Checkpoint | None = None
    retained_user_event_ids: list[UUID] = Field(default_factory=list)


class CommitCompaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: UUID


class CommittedCompaction(BaseModel):
    memory_id: UUID
    version: int
    session_revision: int
    through_seq: int
    method: CompactionMethod = "legacy"


class ContextReference(BaseModel):
    kind: SectionKind
    ref_id: str
    record_type: str | None = None
    version: int | None = None
    revision: int | None = None
    tokens: int = 0
    candidate_tokens: int | None = None
    truncated: bool = False
    reason: str
    decision: Literal["selected", "truncated", "dropped"] | None = None
    decision_reason: str | None = None
    score: float | None = None
    related: list[UUID] = Field(default_factory=list)
    compaction_method: CompactionMethod | None = None
    graph_edge_id: UUID | None = None


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
    pressure: Pressure = "normal"
    pressure_ratio: float = 0.0


class BundleMetadata(BaseModel):
    bundle_id: UUID
    sections: list[ContextSection]
    total_tokens: int
    created_at: AwareDatetime
    pressure: Pressure = "normal"
    pressure_ratio: float = 0.0
