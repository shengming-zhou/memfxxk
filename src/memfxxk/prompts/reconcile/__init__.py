from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

VERSION = "mem0-classic-reconcile-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class FactAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    event: Literal["ADD", "UPDATE", "DELETE", "NONE"]
    id: int | None = None
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    old_memory: str | None = Field(default=None, max_length=4000)


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: list[FactAction] = Field(max_length=12)


SCHEMA = TypeAdapter(Reconciliation)
