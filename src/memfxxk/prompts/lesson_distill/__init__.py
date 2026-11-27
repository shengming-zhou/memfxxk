from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "reasoningbank-lessons-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class LessonDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=6000)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=50)


class Lessons(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lessons: list[LessonDraft] = Field(default_factory=list, max_length=3)


SCHEMA = TypeAdapter(Lessons)
