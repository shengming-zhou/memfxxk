from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "graphiti-node-summary-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    summary: str = Field(min_length=1, max_length=1400)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=80)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summaries: list[Summary] = Field(max_length=32)


SCHEMA = TypeAdapter(Result)
