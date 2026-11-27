from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "story-consistency-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=32)
    excerpt: str = Field(min_length=1, max_length=500)
    source_excerpt: str = Field(min_length=1, max_length=500)
    explanation: str = Field(min_length=1, max_length=500)
    start: int | None = Field(default=None, ge=0)


class Findings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflicts: list[Finding] = Field(max_length=24)


SCHEMA = TypeAdapter(Findings)
