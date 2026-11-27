from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from memfxxk.llm.settings import LLMSettings, Task, TaskModel


class EmbeddingSettings(BaseModel):
    provider: Literal["fake", "openai_compatible"] = "fake"
    base_url: str = "http://localhost:8000/v1"
    model: str = "bge-m3"
    dim: int = Field(default=1024, gt=0)
    api_key: SecretStr | None = None
    batch_size: int = Field(default=64, gt=0)
    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_input_tokens: int = Field(default=8000, gt=0)


class SearchSettings(BaseModel):
    vector_backend: Literal["pgvector", "qdrant"] = "pgvector"
    n_lexical: int = Field(default=50, gt=0)
    n_vector: int = Field(default=50, gt=0)
    m_important: int = Field(default=10, gt=0)
    hnsw_ef_search: int = Field(default=100, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    lexical_weight: float = Field(default=1.0, ge=0)
    vector_weight: float = Field(default=1.0, ge=0)
    important_boost: float = Field(default=1.5, ge=0)
    important_min_sim: float = Field(default=0.35, ge=-1, le=1)
    max_related: int = Field(default=3, ge=0)


class QdrantSettings(BaseModel):
    base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:6333")
    api_key: SecretStr | None = None
    collection_prefix: str = Field(default="memfxxk", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    timeout: float = Field(default=3.0, gt=0, le=30)
    max_prefilter_ids: int = Field(default=5000, gt=0, le=20_000)

    @model_validator(mode="after")
    def validate_endpoint(self) -> "QdrantSettings":
        if self.base_url.username or self.base_url.password or self.base_url.query is not None:
            raise ValueError("Set Qdrant credentials with MEMFXXK_QDRANT__API_KEY")
        if self.base_url.fragment is not None:
            raise ValueError("Qdrant URL must not contain a fragment")
        return self


class ContextBudget(BaseModel):
    documents: int = Field(default=1500, ge=0)
    pinned: int = Field(default=500, ge=0)
    working: int = Field(default=3000, ge=0)
    evidence: int = Field(default=1500, ge=0)


class ContextSettings(BaseModel):
    budget: ContextBudget = Field(default_factory=ContextBudget)
    pressure_threshold: float = Field(default=0.7, gt=0, le=1)


class WorkerSettings(BaseModel):
    embedded: bool = True


class EvalModelSettings(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    model: str = ""
    api_key: SecretStr | None = None
    timeout: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0)


class EvalSettings(BaseModel):
    gen: EvalModelSettings = Field(default_factory=EvalModelSettings)
    judge: EvalModelSettings = Field(default_factory=EvalModelSettings)
    concurrency: int = Field(default=4, gt=0)
    official_repo: Path | None = None


class M2Settings(BaseModel):
    model: EvalModelSettings = Field(default_factory=EvalModelSettings)
    extract_facts: bool = False
    draft_retention_hours: int = Field(default=24, gt=0)
    compact_input_tokens: int = Field(default=128000, gt=0)
    recent_user_tokens: int = Field(default=1024, ge=0)


FactStrategy = Literal["off", "key_proposal", "mem0_classic", "mem0_additive"]


class StrategiesSettings(BaseModel):
    facts: FactStrategy | None = None
    mem0_additive_ranking: bool = False
    graph: Literal["off", "links", "graphiti"] = "links"
    graph_reranker: Literal["rrf", "mmr", "node_distance", "episode_mentions"] = "rrf"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMFXXK_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/memfxxk"
    test_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/memfxxk_test"
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    token_counter: Literal["tiktoken", "simple"] = "tiktoken"
    search: SearchSettings = Field(default_factory=SearchSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    eval: EvalSettings = Field(default_factory=EvalSettings)
    m2: M2Settings = Field(default_factory=M2Settings)
    strategies: StrategiesSettings = Field(default_factory=StrategiesSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    mcp_enable_host: bool = False
    mcp_enable_manage: bool = False
    bundle_retention_days: int = Field(default=30, gt=0)
    allow_host_hard_delete: bool = True

    def task_model(self, task: Task) -> TaskModel:
        legacy = self.m2.model if task in ("compact", "extract") else None
        return self.llm.resolve(task, legacy)

    @property
    def fact_strategy(self) -> FactStrategy:
        if self.strategies.facts is not None:
            return self.strategies.facts
        return "key_proposal" if self.m2.extract_facts else "off"

    @model_validator(mode="after")
    def strategies_require_models(self) -> "Settings":
        if self.fact_strategy != "off" and not self.task_model("extract").model.strip():
            raise ValueError("Fact extraction requires a configured extract model")
        if self.fact_strategy == "mem0_classic" and not self.task_model("reconcile").model.strip():
            raise ValueError("Mem0 classic requires a configured reconcile model")
        if self.strategies.graph == "graphiti":
            graph_tasks: tuple[Task, ...] = (
                "entity_extract",
                "entity_resolve",
                "edge_extract",
                "edge_resolve",
                "node_summary",
            )
            missing = [task for task in graph_tasks if not self.task_model(task).model.strip()]
            if missing:
                raise ValueError(f"Graphiti requires configured models for {', '.join(missing)}")
        return self
