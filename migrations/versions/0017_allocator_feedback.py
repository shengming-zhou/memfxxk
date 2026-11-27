"""Store scoped allocation state and replayable observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _scope() -> tuple[sa.Column, ...]:
    return (
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("agent_id", sa.Text()),
        sa.Column("project_id", sa.Text()),
    )


def upgrade() -> None:
    op.create_table(
        "allocation_states",
        sa.Column("id", sa.UUID(), primary_key=True),
        *_scope(),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("settings", JSONB(), nullable=False),
    )
    op.create_index(
        "ix_allocation_states_scope",
        "allocation_states",
        ["org_id", "project_id", "user_id", "agent_id"],
    )
    op.create_table(
        "allocation_changes",
        *_scope(),
        sa.Column(
            "state_id",
            sa.UUID(),
            sa.ForeignKey("allocation_states.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("bundle_id", sa.UUID()),
        sa.Column("signals", JSONB(), nullable=False),
        sa.Column("reset", sa.Boolean(), nullable=False),
        sa.Column("base_decay", sa.Float(), nullable=False),
        sa.Column("base_expand_items", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_allocation_changes_bundle", "allocation_changes", ["state_id", "bundle_id"])


def downgrade() -> None:
    op.drop_table("allocation_changes")
    op.drop_table("allocation_states")
