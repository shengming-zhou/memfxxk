from importlib.resources import files
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "graphiti-edge-extract-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: int
    target: int
    relation_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    fact: str = Field(min_length=1, max_length=2000)
    valid_at: AwareDatetime | None = None
    invalid_at: AwareDatetime | None = None
    source_event_ids: list[UUID] = Field(min_length=1, max_length=16)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edges: list[Relationship] = Field(max_length=24)


SCHEMA = TypeAdapter(Result)
