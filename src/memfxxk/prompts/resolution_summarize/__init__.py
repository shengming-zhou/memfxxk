from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "source-resolution-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Excerpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: list[str] = Field(max_length=12)
    line: str = Field(max_length=4000)


SCHEMA = TypeAdapter(Excerpts)
