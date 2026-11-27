from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "amem-note-link-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggested_connections: list[int] = Field(max_length=10)


SCHEMA = TypeAdapter(Links)
