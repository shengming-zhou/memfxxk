from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from memfxxk.domain import Actor, Scope

EventKind = Literal["message", "tool_call", "tool_result", "document_import", "user_op"]
EventRole = Literal["user", "assistant", "system", "tool"]


class IngestEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    role: EventRole | None = None
    content: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    event_time: AwareDatetime | None = None
    source: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str | None = None
    scope_drop: set[str] = Field(default_factory=set)


class IngestResult(BaseModel):
    event_id: UUID
    session_seq: int | None = None
    duplicate: bool = False
    memory_ids_touched: list[UUID] = Field(default_factory=list)


class Event(Scope):
    id: UUID
    session_id: str | None
    session_seq: int | None
    turn_id: str | None
    kind: EventKind
    role: EventRole | None
    content: str | None
    payload: dict[str, object] | None
    source: dict[str, object]
    actor: Actor
    event_time: AwareDatetime
    recorded_at: AwareDatetime
    idempotency_key: str | None
    redacted: bool
