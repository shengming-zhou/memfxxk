"""Store enhanced note vectors, revision history and automatic link provenance."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0010"
down_revision = "0009"
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
        "amem_revisions",
        *_scope(),
        sa.Column(
            "record_id", sa.UUID, sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("attributes", JSONB, nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.Column("embedding_model", sa.Text, nullable=False),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("llm_call_id", sa.UUID),
        _created(),
    )
    op.create_table(
        "amem_links",
        *_scope(),
        sa.Column(
            "relation_id",
            sa.UUID,
            sa.ForeignKey("memory_relations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column("llm_call_id", sa.UUID, nullable=False),
        _created(),
    )
    for table in ("amem_revisions", "amem_links"):
        op.create_index(f"ix_{table}_scope", table, ["org_id", "project_id", "user_id", "agent_id"])
        op.create_index(f"ix_{table}_sources", table, ["source_event_ids"], postgresql_using="gin")


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM memory_relations WHERE id IN (SELECT relation_id FROM amem_links)")
    )
    op.execute(
        sa.text(
            "UPDATE records SET metadata = metadata - 'amem' - 'amem_seq' "
            "WHERE metadata ? 'amem' OR metadata ? 'amem_seq'"
        )
    )
    op.drop_table("amem_links")
    op.drop_table("amem_revisions")
