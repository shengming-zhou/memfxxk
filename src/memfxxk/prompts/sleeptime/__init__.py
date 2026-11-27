from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from memfxxk.domain.facts import FactCandidate

VERSION = "letta-sleeptime-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class BlockReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    base_revision: int = Field(ge=0)
    value: str = Field(max_length=100_000)
    source_event_ids: list[UUID] = Field(min_length=1, max_length=50)


class Consolidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[BlockReplacement] = Field(default_factory=list, max_length=20)
    facts: list[FactCandidate] = Field(default_factory=list, max_length=20)


SCHEMA = TypeAdapter(Consolidation)
