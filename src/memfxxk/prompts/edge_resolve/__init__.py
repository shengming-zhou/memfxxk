from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "graphiti-edge-resolve-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_facts: list[int] = Field(default_factory=list, max_length=20)
    contradicted_facts: list[int] = Field(default_factory=list, max_length=20)


SCHEMA = TypeAdapter(Resolution)
