"""Add revisioned core memory blocks and reviewable replacements."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0011"
down_revision = "0010"
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
        "memory_blocks",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("read_only", sa.Boolean, nullable=False),
        sa.Column("limit_chars", sa.Integer, nullable=False),
        sa.Column("head_revision", sa.Integer, nullable=False),
        sa.Column("revision_seq", sa.Integer, nullable=False),
        _created(),
    )
    op.create_index(
        "uq_memory_block_label",
        "memory_blocks",
        [
            "org_id",
            sa.text("coalesce(project_id, '')"),
            sa.text("coalesce(user_id, '')"),
            sa.text("coalesce(agent_id, '')"),
            "label",
        ],
        unique=True,
    )
    op.create_table(
        "block_revisions",
        sa.Column(
            "block_id",
            sa.UUID,
            sa.ForeignKey("memory_blocks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column("actor", JSONB, nullable=False),
        _created(),
    )
    op.create_table(
        "block_proposals",
        sa.Column("id", sa.UUID, primary_key=True),
        *_scope(),
        sa.Column(
            "block_id",
            sa.UUID,
            sa.ForeignKey("memory_blocks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base_revision", sa.Integer, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("adopted_revision", sa.Integer),
        _created(),
    )
    for table in ("memory_blocks", "block_proposals"):
        op.create_index(f"ix_{table}_scope", table, ["org_id", "project_id", "user_id", "agent_id"])
    for table in ("block_revisions", "block_proposals"):
        op.create_index(f"ix_{table}_sources", table, ["source_event_ids"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("block_proposals")
    op.drop_table("block_revisions")
    op.drop_table("memory_blocks")
