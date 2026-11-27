from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class NoteAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1, max_length=1600)
    keywords: list[str] = Field(default_factory=list, max_length=16)
    tags: list[str] = Field(default_factory=list, max_length=16)


class NoteProjection(NoteAttributes):
    revision: int = Field(gt=0)


class RollbackNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(gt=0)
    target_revision: int = Field(gt=0)


class NoteHistoryEntry(BaseModel):
    revision: int
    attributes: NoteAttributes
    operation: Literal["enrich", "evolve", "rollback"]
    source_event_ids: list[UUID]
    created_at: AwareDatetime
