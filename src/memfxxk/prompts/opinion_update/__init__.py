from importlib.resources import files
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "hindsight-opinion-update-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    effect: Literal["strengthens", "weakens", "contradicts", "irrelevant"]


class OpinionUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[Assessment] = Field(default_factory=list, max_length=20)


SCHEMA = TypeAdapter(OpinionUpdates)
