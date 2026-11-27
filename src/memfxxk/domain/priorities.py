from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from memfxxk.domain import Scope


class PriorityTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project", "directory", "document", "section", "record"]
    path: str | None = Field(default=None, min_length=1)
    heading: str | None = Field(default=None, min_length=1)
    memory_id: UUID | None = None

    @model_validator(mode="after")
    def selectors(self) -> Self:
        if (
            (self.kind in ("directory", "document", "section")) != (self.path is not None)
            or (self.kind == "section") != (self.heading is not None)
            or (self.kind == "record") != (self.memory_id is not None)
        ):
            raise ValueError("Provide the path, heading, or memory_id required by the target kind")
        return self


class ClearPriority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PriorityTarget
    scope_drop: set[str] = Field(default_factory=set)


class SetPriority(ClearPriority):
    priority: StrictInt


class PriorityResult(BaseModel):
    target: PriorityTarget
    priority: int
    source: PriorityTarget | None = None
    source_scope: Scope | None = None
