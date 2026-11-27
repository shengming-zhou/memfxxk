from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from memfxxk.prompts.sleeptime import BlockReplacement

VERSION = "memoryos-segment-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8").partition("\n\n")[2]


class SegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=8000)
    blocks: list[BlockReplacement] = Field(default_factory=list, max_length=20)


SCHEMA = TypeAdapter(SegmentUpdate)
