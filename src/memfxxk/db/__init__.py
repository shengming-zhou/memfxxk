from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.engine import Connection, Engine

from .schema import (
    api_keys,
    compactions,
    context_bundles,
    document_revisions,
    documents,
    embedding_cache,
    events,
    fact_proposals,
    jobs,
    memory_marks,
    memory_preferences,
    memory_relations,
    metadata,
    project_key_grants,
    qdrant_replicas,
    record_embeddings,
    records,
    sessions,
)

__all__ = [
    "api_keys",
    "compactions",
    "context_bundles",
    "create_engine",
    "document_revisions",
    "documents",
    "embedding_cache",
    "events",
    "fact_proposals",
    "jobs",
    "memory_marks",
    "memory_preferences",
    "memory_relations",
    "metadata",
    "project_key_grants",
    "qdrant_replicas",
    "record_embeddings",
    "records",
    "sessions",
    "transaction",
]


def create_engine(database_url: str) -> Engine:
    return sqlalchemy_create_engine(database_url, pool_pre_ping=True)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection
