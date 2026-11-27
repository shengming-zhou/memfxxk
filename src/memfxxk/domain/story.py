from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

StoryKey = Annotated[int, Field(ge=-(2**63), lt=2**63)]
TriggerPhrase = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)
]


class LoreKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[TriggerPhrase] = Field(default_factory=list, max_length=24)
    aliases: list[TriggerPhrase] = Field(default_factory=list, max_length=24)


class StoryTime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    start: StoryKey
    end: StoryKey | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError("The story interval must end after its start")
        return self


class StoryAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fact", "edge"]
    id: UUID
    time: StoryTime | None
