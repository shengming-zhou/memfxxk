from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from memfxxk.domain import Scope

Task = Literal[
    "compact",
    "extract",
    "reconcile",
    "entity_extract",
    "entity_resolve",
    "edge_extract",
    "edge_resolve",
    "node_summary",
    "note_enrich",
    "note_link",
    "note_evolve",
    "block_edit",
    "sleeptime",
    "reflect",
    "opinion_update",
    "trajectory_judge",
    "lesson_distill",
    "workflow_induce",
    "rerank",
    "persona_update",
    "resolution_summarize",
    "context_plan",
    "consistency_check",
]


class TaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:8000/v1"
    model: str = ""
    api_key: SecretStr | None = None
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=1500, gt=0)
    timeout: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0)
    response_format: Literal["prompt", "json_object", "json_schema"] = "prompt"


class DailyLimit(Scope):
    tokens: int = Field(ge=0)


class LLMSettings(BaseModel):
    default: TaskModel = Field(default_factory=TaskModel)
    tasks: dict[Task, TaskModel] = Field(default_factory=dict)
    daily_limits: list[DailyLimit] = Field(default_factory=list)
    debug_payloads: bool = False

    def resolve(self, task: Task, legacy: BaseModel | None = None) -> TaskModel:
        overrides = legacy.model_dump(exclude_unset=True) if legacy is not None else {}
        overrides.update(self.default.model_dump(exclude_unset=True))
        if task in self.tasks:
            overrides.update(self.tasks[task].model_dump(exclude_unset=True))
        return TaskModel.model_validate(overrides)
