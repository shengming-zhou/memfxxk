from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from memfxxk.domain import Scope

MemoryType = Literal["turn", "note", "fact", "summary", "lesson", "doc_chunk"]
Origin = Literal["user_explicit", "auto_index", "auto_extract", "import"]
Mark = Literal["important", "pinned"]
Status = Literal["active", "inactive"]
ScopeLevel = Literal["org", "user", "agent", "project"]
ForgetMode = Literal["deactivate", "reactivate", "delete"]
NonEmpty = Annotated[str, Field(min_length=1)]


class Span(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "Span":
        if self.end <= self.start:
            raise ValueError("Span end must follow start")
        return self


class RememberLink(BaseModel):
    memory_id: UUID
    purpose: NonEmpty


class RememberRequest(BaseModel):
    content: NonEmpty | None = None
    title: str | None = None
    from_event_id: UUID | None = None
    span: Span | None = None
    type: Literal["note"] = "note"
    scope_drop: set[str] = Field(default_factory=set)
    important: bool = False
    pinned: bool = False
    links: list[RememberLink] = Field(default_factory=list)
    idempotency_key: NonEmpty | None = None

    @model_validator(mode="after")
    def source_selected(self) -> "RememberRequest":
        if (self.content is None) == (self.from_event_id is None):
            raise ValueError("Provide exactly one of content or from_event_id")
        if self.span is not None and self.from_event_id is None:
            raise ValueError("A span requires from_event_id")
        return self


class PatchRequest(BaseModel):
    base_version: int | None = Field(default=None, gt=0)
    content: NonEmpty | None = None
    title: str | None = None
    important: bool | None = None
    pinned: bool | None = None
    mark_scope_drop: set[str] = Field(default_factory=set)


class LinkRequest(BaseModel):
    from_memory_id: UUID
    to_memory_id: UUID
    purpose: NonEmpty
    note: str | None = None


class ForgetRequest(BaseModel):
    mode: ForgetMode


class MemoryVersion(BaseModel):
    memory_id: UUID
    version: int


class LinkResult(BaseModel):
    relation_id: UUID


class ForgetResult(BaseModel):
    records: int = 0
    embeddings: int = 0
    marks: int = 0
    relations: int = 0
    events_redacted: int = 0


class ListRequest(BaseModel):
    types: list[MemoryType] | None = None
    origin: Origin | None = None
    marks: list[Mark] | None = None
    status: Status = "active"
    session_id: str | None = None
    scope_level: ScopeLevel | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None
    q: str | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None


class MemoryItem(MemoryVersion):
    type: MemoryType
    title: str | None
    snippet: str
    marks: list[Mark]
    status: Status
    origin: Origin
    scope: Scope
    recorded_at: AwareDatetime
    relation_count: int


class MemoryPage(BaseModel):
    items: list[MemoryItem]
    next_cursor: str | None = None
