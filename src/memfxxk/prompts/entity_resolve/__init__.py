from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "graphiti-entity-resolve-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    duplicate_candidate_id: int


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolutions: list[Resolution] = Field(max_length=32)


SCHEMA = TypeAdapter(Result)
