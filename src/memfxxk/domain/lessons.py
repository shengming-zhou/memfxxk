from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

LessonStrategy = Literal["off", "reasoningbank", "awm", "both"]


class FinishTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=8000)
    from_seq: int = Field(default=1, gt=0)
    through_seq: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "FinishTask":
        if self.through_seq is not None and self.through_seq < self.from_seq:
            raise ValueError("through_seq must be on or after from_seq")
        return self


class FinishedTask(BaseModel):
    event_id: UUID
    session_id: str
    from_seq: int
    through_seq: int
    jobs: list[UUID]


class LessonProposal(BaseModel):
    memory_id: UUID
    version: int
    title: str
    content: str
    strategy: Literal["reasoningbank", "awm"]
    decision: Literal["success", "failure"]
    status: Literal["pending", "searchable", "accepted", "rejected"]
    source_event_ids: list[UUID]


class ReviewLesson(BaseModel):
    base_version: int = Field(gt=0)
    accept: bool
