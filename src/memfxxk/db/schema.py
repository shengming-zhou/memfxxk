import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID

metadata = sa.MetaData()


def scope_columns() -> tuple[sa.Column[str], ...]:
    return (
        sa.Column("org_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text),
        sa.Column("agent_id", sa.Text),
        sa.Column("project_id", sa.Text),
    )


def timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def index_scope(table: sa.Table) -> None:
    sa.Index(
        f"ix_{table.name}_scope",
        table.c.org_id,
        table.c.project_id,
        table.c.user_id,
        table.c.agent_id,
    )


tier_segments = sa.Table(
    "tier_segments",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("bank_id", UUID, nullable=False),
    sa.Column("embedding", VECTOR, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("keywords", ARRAY(sa.Text), nullable=False),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False, server_default="{}"),
    sa.Column("generation", sa.Integer, nullable=False, server_default="1"),
    sa.Column("summarized_generation", sa.Integer, nullable=False, server_default="0"),
    sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
    sa.Column("visits", sa.Integer, nullable=False, server_default="0"),
    sa.Column("unpromoted_pages", sa.Integer, nullable=False, server_default="0"),
    sa.Column("promotion_epoch", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_access", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_tier_segments_bank", tier_segments.c.org_id, tier_segments.c.bank_id)
sa.Index("ix_tier_segments_sources", tier_segments.c.source_event_ids, postgresql_using="gin")

tier_pages = sa.Table(
    "tier_pages",
    metadata,
    sa.Column(
        "record_id",
        UUID,
        sa.ForeignKey("records.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("bank_id", UUID, nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("segment_id", UUID, sa.ForeignKey("tier_segments.id", ondelete="SET NULL")),
)
sa.Index("ix_tier_pages_bank_state", tier_pages.c.bank_id, tier_pages.c.state)
sa.Index("ix_tier_pages_segment", tier_pages.c.segment_id)


api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    sa.Column("org_id", sa.Text, nullable=False),
    sa.Column("key_hash", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    timestamp("created_at"),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    sa.CheckConstraint("role IN ('admin', 'host', 'project')", name="ck_api_keys_role"),
)

project_key_grants = sa.Table(
    "project_key_grants",
    metadata,
    sa.Column("key_id", UUID, sa.ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("agent_id", sa.Text),
    sa.Column("access", sa.Text, nullable=False),
    sa.CheckConstraint("access IN ('read', 'write')", name="ck_project_grants_access"),
)

sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("id", sa.Text, nullable=False),
    *scope_columns(),
    sa.Column("next_seq", sa.BigInteger, nullable=False, server_default="1"),
    sa.Column("summary", sa.Text),
    sa.Column("summary_upto_seq", sa.BigInteger),
    sa.Column("state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    timestamp("created_at"),
    timestamp("updated_at"),
    sa.PrimaryKeyConstraint("org_id", "id"),
)
index_scope(sessions)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("session_id", sa.Text),
    sa.Column("session_seq", sa.BigInteger),
    sa.Column("turn_id", sa.Text),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("role", sa.Text),
    sa.Column("content", sa.Text),
    sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("source", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("actor", JSONB, nullable=False),
    sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
    timestamp("recorded_at"),
    sa.Column("idempotency_key", sa.Text),
    sa.Column("idempotency_namespace", sa.Text, nullable=False, server_default=""),
    sa.Column("redacted", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.UniqueConstraint(
        "org_id",
        "idempotency_namespace",
        "idempotency_key",
        name="uq_events_org_namespace_idempotency_key",
    ),
    sa.UniqueConstraint("org_id", "session_id", "session_seq", name="uq_events_session_seq"),
    sa.CheckConstraint(
        "kind IN ('message', 'tool_call', 'tool_result', 'document_import', 'user_op')",
        name="ck_events_kind",
    ),
    sa.CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="ck_events_role"),
)
index_scope(events)

records = sa.Table(
    "records",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    sa.Column("memory_id", UUID, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.true()),
    *scope_columns(),
    sa.Column("session_id", sa.Text),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False, server_default=sa.text("'{}'")),
    sa.Column("source_pos", JSONB),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("confirmed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("valid_from", sa.DateTime(timezone=True)),
    sa.Column("valid_to", sa.DateTime(timezone=True)),
    timestamp("recorded_at"),
    sa.Column("lexemes", TSVECTOR, nullable=False, server_default=sa.text("''::tsvector")),
    sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_by", JSONB, nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("memory_id", "version", name="uq_records_memory_version"),
    sa.CheckConstraint(
        "type IN ('turn', 'note', 'fact', 'summary', 'lesson', 'doc_chunk', 'opinion')",
        name="ck_records_type",
    ),
    sa.CheckConstraint(
        "origin IN ('user_explicit', 'auto_index', 'auto_extract', 'import')",
        name="ck_records_origin",
    ),
    sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_records_status"),
)
index_scope(records)
sa.Index("ix_records_lexemes", records.c.lexemes, postgresql_using="gin")
sa.Index("ix_records_session_event_time", records.c.session_id, records.c.event_time)
sa.Index(
    "uq_records_current_memory",
    records.c.memory_id,
    unique=True,
    postgresql_where=records.c.is_current,
)

record_embeddings = sa.Table(
    "record_embeddings",
    metadata,
    sa.Column("record_id", UUID, sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("model", sa.Text, primary_key=True),
    sa.Column("dim", sa.Integer, nullable=False),
    sa.Column("embedding", VECTOR(), nullable=False),
    timestamp("created_at"),
    sa.CheckConstraint("dim > 0", name="ck_record_embeddings_dim"),
)

embedding_cache = sa.Table(
    "embedding_cache",
    metadata,
    sa.Column("model", sa.Text, primary_key=True),
    sa.Column("text_sha256", sa.Text, primary_key=True),
    sa.Column("dim", sa.Integer, nullable=False),
    sa.Column("embedding", VECTOR(), nullable=False),
    sa.CheckConstraint("dim > 0", name="ck_embedding_cache_dim"),
)

memory_marks = sa.Table(
    "memory_marks",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    sa.Column("memory_id", UUID, nullable=False),
    sa.Column("mark", sa.Text, nullable=False),
    *scope_columns(),
    sa.Column("created_by", JSONB, nullable=False),
    sa.Column("source_event_id", UUID, nullable=False),
    timestamp("created_at"),
    sa.CheckConstraint("mark IN ('important', 'pinned')", name="ck_memory_marks_mark"),
)
index_scope(memory_marks)
sa.Index(
    "uq_memory_marks_scope",
    memory_marks.c.memory_id,
    memory_marks.c.mark,
    memory_marks.c.org_id,
    sa.func.coalesce(memory_marks.c.user_id, ""),
    sa.func.coalesce(memory_marks.c.agent_id, ""),
    sa.func.coalesce(memory_marks.c.project_id, ""),
    unique=True,
)

memory_relations = sa.Table(
    "memory_relations",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    sa.Column("from_memory_id", UUID, nullable=False),
    sa.Column("to_memory_id", UUID, nullable=False),
    sa.Column("purpose", sa.Text, nullable=False),
    sa.Column("note", sa.Text),
    sa.Column("org_id", sa.Text, nullable=False),
    sa.Column("created_by", JSONB, nullable=False),
    sa.Column("source_event_id", UUID, nullable=False),
    timestamp("created_at"),
)
sa.Index("ix_memory_relations_from", memory_relations.c.org_id, memory_relations.c.from_memory_id)
sa.Index("ix_memory_relations_to", memory_relations.c.org_id, memory_relations.c.to_memory_id)

documents = sa.Table(
    "documents",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("purpose", sa.Text, nullable=False),
    sa.Column("load_policy", sa.Text, nullable=False),
    sa.Column("head_revision", sa.Integer, nullable=False),
    timestamp("created_at"),
    timestamp("updated_at"),
    sa.CheckConstraint("load_policy IN ('always', 'on_demand')", name="ck_documents_load_policy"),
)
index_scope(documents)
sa.Index(
    "uq_documents_scope_path",
    documents.c.org_id,
    sa.func.coalesce(documents.c.user_id, ""),
    sa.func.coalesce(documents.c.agent_id, ""),
    sa.func.coalesce(documents.c.project_id, ""),
    documents.c.path,
    unique=True,
)

document_revisions = sa.Table(
    "document_revisions",
    metadata,
    sa.Column(
        "document_id", UUID, sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("author", JSONB, nullable=False),
    sa.Column("base_revision", sa.Integer),
    sa.Column("message", sa.Text),
    timestamp("created_at"),
)

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="queued"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
    timestamp("run_after"),
    sa.Column("locked_by", sa.Text),
    sa.Column("locked_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("dedupe_key", sa.Text),
    timestamp("created_at"),
    timestamp("updated_at"),
    sa.UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
    sa.CheckConstraint(
        "status IN ('queued', 'running', 'done', 'failed', 'dead')", name="ck_jobs_status"
    ),
)
sa.Index("ix_jobs_status_run_after", jobs.c.status, jobs.c.run_after)

context_bundles = sa.Table(
    "context_bundles",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("session_id", sa.Text),
    sa.Column("query_sha256", sa.Text, nullable=False),
    sa.Column("budget", JSONB, nullable=False),
    sa.Column("items", JSONB, nullable=False),
    sa.Column("total_tokens", sa.Integer, nullable=False),
    timestamp("created_at"),
)
index_scope(context_bundles)

allocation_states = sa.Table(
    "allocation_states",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("version", sa.BigInteger, nullable=False),
    sa.Column("settings", JSONB, nullable=False),
)
index_scope(allocation_states)

allocation_changes = sa.Table(
    "allocation_changes",
    metadata,
    *scope_columns(),
    sa.Column(
        "state_id",
        UUID,
        sa.ForeignKey("allocation_states.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("version", sa.BigInteger, primary_key=True),
    sa.Column("bundle_id", UUID),
    sa.Column("signals", JSONB, nullable=False),
    sa.Column("reset", sa.Boolean, nullable=False),
    sa.Column("base_decay", sa.Float, nullable=False),
    sa.Column("base_expand_items", sa.Integer, nullable=False),
    timestamp("created_at"),
)
sa.Index(
    "ix_allocation_changes_bundle", allocation_changes.c.state_id, allocation_changes.c.bundle_id
)

compactions = sa.Table(
    "compactions",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("base_revision", sa.Integer, nullable=False),
    sa.Column("from_seq", sa.BigInteger, nullable=False),
    sa.Column("through_seq", sa.BigInteger, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("summary", sa.Text),
    sa.Column("tokens", sa.Integer, nullable=False),
    sa.Column("memory_id", UUID),
    sa.Column("committed_version", sa.Integer),
    sa.Column("committed_revision", sa.Integer),
    sa.Column("method", sa.Text, nullable=False, server_default="legacy"),
    sa.Column("checkpoint", JSONB),
    sa.Column(
        "retained_user_event_ids", ARRAY(UUID), nullable=False, server_default=sa.text("'{}'")
    ),
    timestamp("created_at"),
)
index_scope(compactions)
sa.Index("ix_compactions_sources", compactions.c.source_event_ids, postgresql_using="gin")
sa.Index("ix_records_sources", records.c.source_event_ids, postgresql_using="gin")

memory_preferences = sa.Table(
    "memory_preferences",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("memory_id", UUID, nullable=False),
    sa.Column("priority", sa.Numeric),
    sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
    sa.CheckConstraint("priority = trunc(priority)", name="ck_memory_preferences_integer"),
)
index_scope(memory_preferences)
sa.Index("ix_memory_preferences_memory", memory_preferences.c.memory_id)
sa.Index(
    "uq_memory_preferences_scope",
    memory_preferences.c.memory_id,
    memory_preferences.c.org_id,
    sa.func.coalesce(memory_preferences.c.user_id, ""),
    sa.func.coalesce(memory_preferences.c.agent_id, ""),
    sa.func.coalesce(memory_preferences.c.project_id, ""),
    unique=True,
)

content_priorities = sa.Table(
    "content_priorities",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("document_id", UUID, sa.ForeignKey("documents.id", ondelete="CASCADE")),
    sa.Column("priority", sa.Numeric, nullable=False),
    sa.CheckConstraint("priority = trunc(priority)", name="ck_content_priorities_integer"),
)
index_scope(content_priorities)

fact_proposals = sa.Table(
    "fact_proposals",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("operation", sa.Text, nullable=False, server_default="propose"),
    sa.Column("base_version", sa.Integer),
    sa.CheckConstraint(
        "(operation = 'propose' AND base_version IS NULL) "
        "OR (operation IN ('update', 'delete') AND base_version > 0 AND base_version IS NOT NULL)",
        name="ck_fact_proposal_operation",
    ),
    sa.Column("fact", JSONB, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("memory_id", UUID),
    sa.Column("memory_version", sa.Integer),
    timestamp("created_at"),
    sa.CheckConstraint(
        "status IN ('pending', 'accepted', 'rejected')", name="ck_fact_proposals_status"
    ),
)
index_scope(fact_proposals)
sa.Index("ix_fact_proposals_memory", fact_proposals.c.memory_id)
sa.Index("ix_fact_proposals_sources", fact_proposals.c.source_event_ids, postgresql_using="gin")

qdrant_replicas = sa.Table(
    "qdrant_replicas",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    sa.Column("record_id", UUID, nullable=False),
    sa.Column("endpoint", sa.Text, nullable=False),
    sa.Column("collection", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("dim", sa.Integer, nullable=False),
    sa.Column("generation", sa.Integer),
    sa.UniqueConstraint("record_id", "endpoint", "collection", name="uq_qdrant_replica_placement"),
    sa.CheckConstraint("dim > 0", name="ck_qdrant_replicas_dim"),
)
sa.Index("ix_qdrant_replicas_endpoint", qdrant_replicas.c.endpoint)

llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("task", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("request_id", sa.UUID),
    sa.Column("job_id", sa.UUID),
    sa.Column("input_sha256", sa.Text, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False, server_default=sa.text("'{}'")),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("reserved_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("retries", sa.Integer, nullable=False, server_default="0"),
    sa.Column("latency_ms", sa.Float, nullable=False, server_default="0"),
    sa.Column("debug_input", JSONB),
    sa.Column("debug_output", JSONB),
    timestamp("created_at"),
    sa.CheckConstraint(
        "status IN ('running', 'succeeded', 'failed', 'budget_exceeded')",
        name="ck_llm_calls_status",
    ),
)
sa.Index("ix_llm_calls_org_day", llm_calls.c.org_id, llm_calls.c.created_at)
sa.Index("ix_llm_calls_sources", llm_calls.c.source_event_ids, postgresql_using="gin")

entities = sa.Table(
    "entities",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("normalized_name", sa.Text, nullable=False),
    sa.Column("entity_type", sa.Text, nullable=False),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    sa.Column("name_vector", VECTOR()),
    sa.Column("embedding_model", sa.Text),
    sa.Column("lexemes", TSVECTOR, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column(
        "summary_source_event_ids", ARRAY(UUID), nullable=False, server_default=sa.text("'{}'")
    ),
    sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
    timestamp("created_at"),
)
index_scope(entities)
sa.Index("ix_entities_lexemes", entities.c.lexemes, postgresql_using="gin")
sa.Index("ix_entities_sources", entities.c.source_event_ids, postgresql_using="gin")

entity_edges = sa.Table(
    "entity_edges",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("source_id", UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
    sa.Column("target_id", UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
    sa.Column("relation_type", sa.Text, nullable=False),
    sa.Column("fact", sa.Text, nullable=False),
    sa.Column("fact_vector", VECTOR()),
    sa.Column("embedding_model", sa.Text),
    sa.Column("lexemes", TSVECTOR, nullable=False),
    sa.Column("valid_at", sa.DateTime(timezone=True)),
    sa.Column("invalid_at", sa.DateTime(timezone=True)),
    sa.Column("declared_invalid_at", sa.DateTime(timezone=True)),
    sa.Column("expired_at", sa.DateTime(timezone=True)),
    sa.Column("asserted_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("supports", JSONB, nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
    sa.Column(
        "invalidation_event_ids", ARRAY(UUID), nullable=False, server_default=sa.text("'{}'")
    ),
    sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
    timestamp("created_at"),
)
index_scope(entity_edges)
sa.Index("ix_entity_edges_source", entity_edges.c.source_id, entity_edges.c.relation_type)
sa.Index("ix_entity_edges_target", entity_edges.c.target_id)
sa.Index("ix_entity_edges_sources", entity_edges.c.source_event_ids, postgresql_using="gin")
sa.Index("ix_entity_edges_lexemes", entity_edges.c.lexemes, postgresql_using="gin")

entity_mentions = sa.Table(
    "entity_mentions",
    metadata,
    *scope_columns(),
    sa.Column(
        "entity_id", UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column(
        "episode_id", UUID, sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("asserted_at", sa.DateTime(timezone=True), nullable=False),
    timestamp("created_at"),
)
index_scope(entity_mentions)
sa.Index("ix_entity_mentions_episode", entity_mentions.c.episode_id)
sa.Index("ix_entity_mentions_sources", entity_mentions.c.source_event_ids, postgresql_using="gin")

amem_revisions = sa.Table(
    "amem_revisions",
    metadata,
    *scope_columns(),
    sa.Column("record_id", UUID, sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("attributes", JSONB, nullable=False),
    sa.Column("embedding", VECTOR(), nullable=False),
    sa.Column("embedding_model", sa.Text, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("operation", sa.Text, nullable=False),
    sa.Column("llm_call_id", UUID),
    sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
    timestamp("created_at"),
)
index_scope(amem_revisions)
sa.Index("ix_amem_revisions_sources", amem_revisions.c.source_event_ids, postgresql_using="gin")

amem_links = sa.Table(
    "amem_links",
    metadata,
    *scope_columns(),
    sa.Column(
        "relation_id",
        UUID,
        sa.ForeignKey("memory_relations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("llm_call_id", UUID, nullable=False),
    timestamp("created_at"),
)
index_scope(amem_links)
sa.Index("ix_amem_links_sources", amem_links.c.source_event_ids, postgresql_using="gin")

memory_blocks = sa.Table(
    "memory_blocks",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("read_only", sa.Boolean, nullable=False),
    sa.Column("limit_chars", sa.Integer, nullable=False),
    sa.Column("head_revision", sa.Integer, nullable=False),
    sa.Column("revision_seq", sa.Integer, nullable=False),
    timestamp("created_at"),
)
index_scope(memory_blocks)
sa.Index(
    "uq_memory_block_label",
    memory_blocks.c.org_id,
    sa.func.coalesce(memory_blocks.c.project_id, ""),
    sa.func.coalesce(memory_blocks.c.user_id, ""),
    sa.func.coalesce(memory_blocks.c.agent_id, ""),
    memory_blocks.c.label,
    unique=True,
)

block_revisions = sa.Table(
    "block_revisions",
    metadata,
    sa.Column(
        "block_id", UUID, sa.ForeignKey("memory_blocks.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("actor", JSONB, nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
    timestamp("created_at"),
)
sa.Index("ix_block_revisions_sources", block_revisions.c.source_event_ids, postgresql_using="gin")

block_proposals = sa.Table(
    "block_proposals",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column(
        "block_id", UUID, sa.ForeignKey("memory_blocks.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("base_revision", sa.Integer, nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("adopted_revision", sa.Integer),
    sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
    timestamp("created_at"),
)
index_scope(block_proposals)
sa.Index("ix_block_proposals_sources", block_proposals.c.source_event_ids, postgresql_using="gin")

content_resolutions = sa.Table(
    "content_resolutions",
    metadata,
    sa.Column("id", UUID, primary_key=True),
    *scope_columns(),
    sa.Column("record_id", UUID, sa.ForeignKey("records.id", ondelete="CASCADE")),
    sa.Column("document_id", UUID, sa.ForeignKey("documents.id", ondelete="CASCADE")),
    sa.Column("block_id", UUID, sa.ForeignKey("memory_blocks.id", ondelete="CASCADE")),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("variants", JSONB, nullable=False),
    sa.Column("source_event_ids", ARRAY(UUID), nullable=False),
    sa.CheckConstraint(
        "num_nonnulls(record_id, document_id, block_id) = 1", name="ck_content_resolutions_source"
    ),
)
sa.Index(
    "ix_content_resolutions_sources", content_resolutions.c.source_event_ids, postgresql_using="gin"
)
for _resolution_fk in ("record_id", "document_id", "block_id"):
    sa.Index(f"ix_content_resolutions_{_resolution_fk}", content_resolutions.c[_resolution_fk])
