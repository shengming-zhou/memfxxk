from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "compact-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class CheckpointItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=16)


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latest_decisions: list[CheckpointItem] = Field(default_factory=list, max_length=16)
    open_tasks: list[CheckpointItem] = Field(default_factory=list, max_length=16)
    identifiers: list[CheckpointItem] = Field(default_factory=list, max_length=16)
    tool_results: list[CheckpointItem] = Field(default_factory=list, max_length=16)
    preferences_and_corrections: list[CheckpointItem] = Field(default_factory=list, max_length=16)
    unverified: list[CheckpointItem] = Field(default_factory=list, max_length=16)


SCHEMA = TypeAdapter(Checkpoint)
