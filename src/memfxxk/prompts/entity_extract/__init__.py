from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "graphiti-entity-extract-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(min_length=1, max_length=80)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=16)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[Mention] = Field(max_length=32)


SCHEMA = TypeAdapter(Result)
