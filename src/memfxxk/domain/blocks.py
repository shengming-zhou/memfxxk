from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

BlockOwner = Literal["user", "agent"]


class CreateBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    value: str = Field(default="", max_length=100_000)
    limit: int = Field(default=2000, gt=0, le=100_000)
    owner: BlockOwner = "user"
    read_only: bool | None = None
    scope_drop: set[str] = Field(default_factory=set)


class EditBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    operation: Literal["append", "replace", "rewrite"]
    text: str = Field(max_length=100_000)
    old_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def replacement_inputs(self) -> Self:
        if (self.operation == "replace") != (self.old_text is not None):
            raise ValueError("old_text is required only for replace")
        return self


class MemoryBlock(BaseModel):
    id: UUID
    label: str
    description: str
    owner: BlockOwner
    owner_id: str
    read_only: bool
    limit: int
    char_count: int
    value: str
    revision: int
    source_event_ids: list[UUID]


class BlockProposal(BaseModel):
    id: UUID
    block_id: UUID
    base_revision: int
    value: str
    source_event_ids: list[UUID]
    status: Literal["pending", "accepted", "rejected"]
    adopted_revision: int | None
    created_at: AwareDatetime


class BlockEditResult(BaseModel):
    block: MemoryBlock
    proposal: BlockProposal | None = None


class ReviewBlock(BaseModel):
    accept: bool
