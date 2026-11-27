"""Add scoped, rebuildable temporal entity and relation projections."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _scope() -> tuple[sa.Column, ...]:
    return (
        sa.Column("org_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text),
        sa.Column("agent_id", sa.Text),
        sa.Column("project_id", sa.Text),
    )


def _created() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("normalized_name", sa.Text, nullable=False),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("name_vector", VECTOR()),
        sa.Column("embedding_model", sa.Text),
        sa.Column("lexemes", TSVECTOR, nullable=False),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column(
            "summary_source_event_ids",
            ARRAY(sa.UUID),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        _created(),
    )
    op.create_table(
        "entity_edges",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column(
            "source_id", sa.UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "target_id", sa.UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
        ),
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
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column("supports", JSONB, nullable=False),
        sa.Column(
            "invalidation_event_ids", ARRAY(sa.UUID), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        _created(),
    )
    op.create_table(
        "entity_mentions",
        *_scope(),
        sa.Column(
            "entity_id", sa.UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "episode_id", sa.UUID, sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column("asserted_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
    )
    for table in ("entities", "entity_edges", "entity_mentions"):
        op.create_index(f"ix_{table}_scope", table, ["org_id", "project_id", "user_id", "agent_id"])
        op.create_index(f"ix_{table}_sources", table, ["source_event_ids"], postgresql_using="gin")
    for table in ("entities", "entity_edges"):
        op.create_index(f"ix_{table}_lexemes", table, ["lexemes"], postgresql_using="gin")
    op.create_index("ix_entity_edges_source", "entity_edges", ["source_id", "relation_type"])
    op.create_index("ix_entity_edges_target", "entity_edges", ["target_id"])
    op.create_index("ix_entity_mentions_episode", "entity_mentions", ["episode_id"])


def downgrade() -> None:
    op.drop_table("entity_mentions")
    op.drop_table("entity_edges")
    op.drop_table("entities")
