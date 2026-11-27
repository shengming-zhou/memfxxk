from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from memfxxk.domain.context import SectionKind

FeedbackKind = Literal["retrieved", "unused", "conflict", "correction"]


class FeedbackReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SectionKind
    ref_id: str = Field(min_length=1, max_length=256)
    section_index: int | None = Field(default=None, ge=0)


class FeedbackObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal: FeedbackKind
    target: FeedbackReference


class FeedbackReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: UUID
    assistant_event_id: UUID | None = None
    observations: list[FeedbackObservation] = Field(default_factory=list, max_length=100)


class AllocationState(BaseModel):
    version: int = 0
    multipliers: dict[int, float] = Field(default_factory=dict)
    decay: float
    expand_items: int


class LearningSignal(FeedbackObservation):
    priority: int


class AllocationChange(BaseModel):
    version: int
    bundle_id: UUID | None = None
    signals: list[LearningSignal]
    reset: bool
    base_decay: float
    base_expand_items: int
    created_at: AwareDatetime
