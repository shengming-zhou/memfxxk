from importlib.resources import files

from pydantic import TypeAdapter

from memfxxk.domain.notes import NoteAttributes

VERSION = "amem-note-enrich-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")
SCHEMA = TypeAdapter(NoteAttributes)
