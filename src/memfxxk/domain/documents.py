from re import fullmatch
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from memfxxk.domain import Actor, Scope
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.story import LoreKeys

LoadPolicy = Literal["always", "on_demand"]


def document_path(path: str) -> str:
    parts = path.split("/")
    if not fullmatch(r"[A-Za-z0-9._/-]+\.md", path) or path.startswith("/") or ".." in parts:
        raise ServiceError(422, "invalid_document_path", "Use a relative Markdown path")
    return "/".join(part for part in parts if part and part != ".")


class PutDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    base_revision: int | None = Field(default=None, ge=0)
    purpose: str | None = None
    load_policy: LoadPolicy | None = None
    scope_drop: set[str] = Field(default_factory=set)
    message: str | None = None
    lore: dict[str, LoreKeys] | None = Field(default=None, max_length=100)


class DocumentInfo(BaseModel):
    document_id: UUID
    path: str
    scope: Scope
    purpose: str
    load_policy: LoadPolicy
    revision: int
    updated_at: AwareDatetime


class Document(DocumentInfo):
    content: str
    lore: dict[str, LoreKeys] = Field(default_factory=dict)


class DocumentRevision(BaseModel):
    revision: int
    author: Actor
    base_revision: int | None
    message: str | None
    created_at: AwareDatetime
