from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "trajectory-judge-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["success", "failure", "unclear"]
    rationale: str = Field(min_length=1, max_length=2000)


SCHEMA = TypeAdapter(Judgment)
