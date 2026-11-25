from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class ContextBudget(BaseModel):
    documents: int = Field(default=1500, ge=0)
    pinned: int = Field(default=500, ge=0)
    working: int = Field(default=3000, ge=0)
    evidence: int = Field(default=1500, ge=0)


class ContextSettings(BaseModel):
    budget: ContextBudget = Field(default_factory=ContextBudget)


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

    @model_validator(mode="after")
    def model_required_for_extraction(self) -> "M2Settings":
        if self.extract_facts and not self.model.model.strip():
            raise ValueError("Fact extraction requires MEMFXXK_M2__MODEL__MODEL")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMFXXK_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/memfxxk"
    test_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/memfxxk_test"
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    token_counter: Literal["tiktoken", "simple"] = "tiktoken"
    search: SearchSettings = Field(default_factory=SearchSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    eval: EvalSettings = Field(default_factory=EvalSettings)
    m2: M2Settings = Field(default_factory=M2Settings)
    mcp_enable_host: bool = False
    mcp_enable_manage: bool = False
    bundle_retention_days: int = Field(default=30, gt=0)
    allow_host_hard_delete: bool = True
