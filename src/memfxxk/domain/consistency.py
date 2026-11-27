from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from memfxxk.domain.feedback import FeedbackReference
from memfxxk.domain.resolutions import TextResolution


class ConsistencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=40_000)
    min_priority: int | None = None
    evidence_tokens: int = Field(default=2400, ge=32, le=16_000)
    graph_hops: int = Field(default=0, ge=0, le=2)


class ConsistencyConflict(BaseModel):
    target: FeedbackReference
    version: int | None = None
    revision: int | None = None
    graph_edge_id: UUID | None = None
    start: int
    end: int
    excerpt: str
    source_excerpt: str
    explanation: str


class ConsistencyResult(BaseModel):
    conflicts: list[ConsistencyConflict]
    checked: list[FeedbackReference]
    min_priority: int | None = None
    llm_call_id: UUID | None = None


class OutlineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1, max_length=100)
    session_ids: list[str] = Field(default_factory=list, max_length=32)
    max_tokens: int = Field(default=512, ge=1, le=2048)


class OutlineSection(BaseModel):
    index: int
    heading: str
    summary: TextResolution
    line: TextResolution


class ChapterOutline(BaseModel):
    id: UUID
    generation: int
    sections: list[OutlineSection]


class OutlineResult(BaseModel):
    chapters: list[ChapterOutline]
    checkpoint_ids: list[UUID] = Field(default_factory=list)
    summary: TextResolution
    line: TextResolution
