from importlib.resources import files
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from memfxxk.domain.reflect import OpinionSuggestion

VERSION = "hindsight-reflect-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Recall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["recall"]
    query: str = Field(min_length=1, max_length=1200)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["answer"]
    answer: str = Field(min_length=1, max_length=16_000)
    citations: list[UUID] = Field(default_factory=list, max_length=40)
    opinions: list[OpinionSuggestion] = Field(default_factory=list, max_length=10)


SCHEMA = TypeAdapter(Annotated[Recall | Answer, Field(discriminator="action")])
