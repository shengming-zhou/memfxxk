from importlib.resources import files
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

CLASSIC_VERSION = "mem0-classic-extract-v1"
ADDITIVE_VERSION = "mem0-additive-extract-v2"
CLASSIC_PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")
ADDITIVE_PROMPT = files(__package__).joinpath("v2.md").read_text(encoding="utf-8")


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4000)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=12)
    valid_from: AwareDatetime | None = None


class ClassicFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[ExtractedFact] = Field(max_length=12)


class AdditiveFact(ExtractedFact):
    attributed_to: Literal["user", "assistant"]
    linked_memory_ids: list[int] = Field(default_factory=list, max_length=10)
    entities: list[str] = Field(default_factory=list, max_length=8)


class AdditiveFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: list[AdditiveFact] = Field(max_length=12)


CLASSIC_SCHEMA = TypeAdapter(ClassicFacts)
ADDITIVE_SCHEMA = TypeAdapter(AdditiveFacts)
