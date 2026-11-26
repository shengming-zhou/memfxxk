from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Scope(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: str
    user_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None


class ProjectGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1)
    access: Literal["read", "write"]


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: str
    role: Literal["admin", "host", "project"]
    project_grant: ProjectGrant | None = None

    @model_validator(mode="after")
    def validate_grant(self) -> "Principal":
        if (self.role == "project") != (self.project_grant is not None):
            raise ValueError("Project credentials require a project grant")
        return self


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["user", "agent", "system", "import"]
    id: str | None = None


class RequestContext(Scope):
    actor: Actor

    @model_validator(mode="before")
    @classmethod
    def default_actor(cls, data: object) -> object:
        if isinstance(data, Mapping) and "actor" not in data:
            return {**data, "actor": {"type": "agent", "id": data.get("agent_id")}}
        return data
