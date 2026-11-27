"""Extend record priorities and store scoped priorities for Markdown content.

Downgrading removes directory, document, section, and project defaults. Explicit
record priorities are clamped to 1–5, while existing nulls and tags are preserved.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_memory_preferences_priority", "memory_preferences", type_="check")
    op.alter_column("memory_preferences", "priority", type_=sa.Numeric())
    op.create_check_constraint(
        "ck_memory_preferences_integer", "memory_preferences", "priority = trunc(priority)"
    )
    op.create_table(
        "content_priorities",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("agent_id", sa.Text()),
        sa.Column("project_id", sa.Text()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("priority", sa.Numeric(), nullable=False),
        sa.CheckConstraint("priority = trunc(priority)", name="ck_content_priorities_integer"),
    )
    op.create_index(
        "ix_content_priorities_scope",
        "content_priorities",
        [
            "org_id",
            "project_id",
            "user_id",
            "agent_id",
        ],
    )


def downgrade() -> None:
    op.drop_table("content_priorities")
    op.drop_constraint("ck_memory_preferences_integer", "memory_preferences", type_="check")
    op.execute(
        "UPDATE memory_preferences SET priority = greatest(1, least(5, priority)) "
        "WHERE priority IS NOT NULL AND priority NOT BETWEEN 1 AND 5"
    )
    op.alter_column(
        "memory_preferences", "priority", type_=sa.Integer(), postgresql_using="priority::integer"
    )
    op.create_check_constraint(
        "ck_memory_preferences_priority", "memory_preferences", "priority BETWEEN 1 AND 5"
    )
