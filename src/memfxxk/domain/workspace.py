from pathlib import PureWindowsPath
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from memfxxk.domain.documents import LoadPolicy, document_path
from memfxxk.domain.errors import ServiceError


def portable_path(value: str) -> str:
    try:
        canonical = document_path(value)
    except ServiceError as exc:
        raise ValueError("Use a relative Markdown path") from exc
    if canonical != value or any(
        part.endswith(".") or PureWindowsPath(part).is_reserved() for part in value.split("/")
    ):
        raise ValueError("Use a canonical, portable Markdown path")
    return canonical


class WorkspaceDocumentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    purpose: str | None = None
    load_policy: LoadPolicy | None = None
    document_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return portable_path(value)

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        if (self.document_id is None) != (self.revision is None):
            raise ValueError("Document ID and revision must be supplied together")
        return self


class WorkspaceDocument(WorkspaceDocumentInfo):
    content: str


class WorkspaceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_time: AwareDatetime
    content: str | None = None
    payload: dict[str, object]


class WorkspaceLessonInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: UUID
    version: int = Field(ge=1)
    title: str | None = None
    failure: WorkspaceEvidence
    verification: WorkspaceEvidence

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if (
            self.failure.event_id == self.verification.event_id
            or self.failure.payload.get("outcome") != "failure"
            or self.verification.payload.get("outcome") != "success"
        ):
            raise ValueError("Provide distinct failure and success tool results")
        return self


class WorkspaceLesson(WorkspaceLessonInfo):
    content: str


def validate_paths(values: list[str]) -> None:
    paths = {value.casefold() for value in values}
    if len(paths) != len(values):
        raise ValueError("Workspace contains conflicting Markdown paths")
    for path in paths:
        if any(parent.as_posix() in paths for parent in PureWindowsPath(path).parents):
            raise ValueError("A Markdown path conflicts with a directory")


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    documents: list[WorkspaceDocumentInfo] = Field(default_factory=list)
    lessons: list[WorkspaceLessonInfo] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        validate_paths([doc.path for doc in self.documents])
        if len({lesson.memory_id for lesson in self.lessons}) != len(self.lessons):
            raise ValueError("Workspace contains duplicate lessons")
        return self


class WorkspaceSnapshot(WorkspaceManifest):
    documents: list[WorkspaceDocument] = Field(default_factory=list)
    lessons: list[WorkspaceLesson] = Field(default_factory=list)


class ImportWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: WorkspaceSnapshot
    update_existing: bool = False


class WorkspaceImportResult(BaseModel):
    documents_created: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    lessons_created: int = 0
    lessons_unchanged: int = 0
