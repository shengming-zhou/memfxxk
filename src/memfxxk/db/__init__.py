from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.engine import Connection, Engine

from .schema import (
    allocation_changes,
    allocation_states,
    amem_links,
    amem_revisions,
    api_keys,
    block_proposals,
    block_revisions,
    compactions,
    content_priorities,
    content_resolutions,
    context_bundles,
    document_revisions,
    documents,
    embedding_cache,
    entities,
    entity_edges,
    entity_mentions,
    events,
    fact_proposals,
    jobs,
    llm_calls,
    memory_blocks,
    memory_marks,
    memory_preferences,
    memory_relations,
    metadata,
    project_key_grants,
    qdrant_replicas,
    record_embeddings,
    records,
    sessions,
    story_annotations,
    tier_pages,
    tier_segments,
)

__all__ = [
    "allocation_changes",
    "allocation_states",
    "amem_links",
    "amem_revisions",
    "api_keys",
    "block_proposals",
    "block_revisions",
    "compactions",
    "content_priorities",
    "content_resolutions",
    "context_bundles",
    "create_engine",
    "document_revisions",
    "documents",
    "embedding_cache",
    "entities",
    "entity_edges",
    "entity_mentions",
    "events",
    "fact_proposals",
    "jobs",
    "llm_calls",
    "memory_marks",
    "memory_blocks",
    "memory_preferences",
    "memory_relations",
    "metadata",
    "project_key_grants",
    "qdrant_replicas",
    "record_embeddings",
    "records",
    "sessions",
    "story_annotations",
    "tier_pages",
    "tier_segments",
    "transaction",
]


def create_engine(database_url: str) -> Engine:
    return sqlalchemy_create_engine(database_url, pool_pre_ping=True)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection
