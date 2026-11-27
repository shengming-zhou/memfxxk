from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from memfxxk.domain.notes import NoteAttributes

VERSION = "amem-note-evolve-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")


class Evolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_evolve: bool
    actions: list[Literal["update_neighbor"]]
    attributes: NoteAttributes | None = None


SCHEMA = TypeAdapter(Evolution)
