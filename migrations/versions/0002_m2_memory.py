"""Add scoped compaction drafts, preferences and fact proposals.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _scope() -> tuple[sa.Column, ...]:
    return (
        sa.Column("org_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text),
        sa.Column("agent_id", sa.Text),
        sa.Column("project_id", sa.Text),
    )


def upgrade() -> None:
    op.create_table(
        "compactions",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("base_revision", sa.Integer, nullable=False),
        sa.Column("from_seq", sa.BigInteger, nullable=False),
        sa.Column("through_seq", sa.BigInteger, nullable=False),
        sa.Column("source_event_ids", postgresql.ARRAY(sa.UUID), nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("tokens", sa.Integer, nullable=False),
        sa.Column("memory_id", sa.UUID),
        sa.Column("committed_version", sa.Integer),
        sa.Column("committed_revision", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "memory_preferences",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column("memory_id", sa.UUID, nullable=False),
        sa.Column("priority", sa.Integer),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 5", name="ck_memory_preferences_priority"),
    )
    op.create_table(
        "fact_proposals",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column("source_event_ids", postgresql.ARRAY(sa.UUID), nullable=False),
        sa.Column("fact", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("memory_id", sa.UUID),
        sa.Column("memory_version", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_fact_proposals_status"
        ),
    )
    for table in ("compactions", "fact_proposals", "memory_preferences"):
        op.create_index(f"ix_{table}_scope", table, ["org_id", "project_id", "user_id", "agent_id"])
    for table in ("compactions", "fact_proposals", "records"):
        op.create_index(f"ix_{table}_sources", table, ["source_event_ids"], postgresql_using="gin")
    op.create_index("ix_memory_preferences_memory", "memory_preferences", ["memory_id"])
    op.create_index("ix_fact_proposals_memory", "fact_proposals", ["memory_id"])
    op.create_index(
        "uq_memory_preferences_scope",
        "memory_preferences",
        [
            "memory_id",
            "org_id",
            sa.text("coalesce(user_id, '')"),
            sa.text("coalesce(agent_id, '')"),
            sa.text("coalesce(project_id, '')"),
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_records_sources", table_name="records")
    for table in ("fact_proposals", "memory_preferences", "compactions"):
        op.drop_table(table)
