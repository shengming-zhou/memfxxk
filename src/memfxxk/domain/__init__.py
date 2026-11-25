from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class Scope(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: str
    user_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    org_id: str
    role: Literal["admin", "host"]


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
