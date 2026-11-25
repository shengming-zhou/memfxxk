from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from memfxxk.domain import Scope


class SessionState(BaseModel):
    session_id: str
    scope: Scope
    last_seq: int
    summary: str | None
    summary_upto_seq: int | None
    state: dict[str, object]
    revision: int
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PutSessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    state: dict[str, object] = Field(default_factory=dict)
    summary: str | None = None
    summary_upto_seq: int | None = Field(default=None, ge=0)
