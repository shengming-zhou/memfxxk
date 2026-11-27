from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "relevance-rerank-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Ranking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: list[int] = Field(default_factory=list, max_length=100)


SCHEMA = TypeAdapter(Ranking)
