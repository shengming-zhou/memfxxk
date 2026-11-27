from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResolutionSource = Literal["record", "document", "block"]
ResolutionLevel = Literal["full", "summary", "line"]


class ResolutionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ResolutionSource
    id: UUID | None = None
    path: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def select_source(self) -> Self:
        if self.kind == "document" and self.path and self.id is None:
            return self
        if self.kind != "document" and self.id is not None and self.path is None:
            return self
        raise ValueError("Select a document path or one record/block ID")


class TextResolution(BaseModel):
    text: str
    tokens: int
    method: Literal["original", "extractive_fallback", "llm_extract"]


class ResolutionPair(BaseModel):
    summary: TextResolution
    line: TextResolution


class ContentSection(BaseModel):
    index: int
    heading: str
    full: TextResolution
    summary: TextResolution
    line: TextResolution


class ContentResolutions(BaseModel):
    id: UUID
    generation: int
    sections: list[ContentSection]
