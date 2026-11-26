"""Bind direct credentials to a project member.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_api_keys_role", "api_keys", type_="check")
    op.create_check_constraint(
        "ck_api_keys_role", "api_keys", "role IN ('admin', 'host', 'project')"
    )
    op.create_table(
        "project_key_grants",
        sa.Column(
            "key_id", sa.UUID, sa.ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text),
        sa.Column("access", sa.Text, nullable=False),
        sa.CheckConstraint("access IN ('read', 'write')", name="ck_project_grants_access"),
    )


def downgrade() -> None:
    # A scoped credential must never become an active unrestricted host credential.
    op.execute(
        "UPDATE api_keys SET role = 'host', revoked_at = coalesce(revoked_at, now()) "
        "WHERE role = 'project'"
    )
    op.drop_table("project_key_grants")
    op.drop_constraint("ck_api_keys_role", "api_keys", type_="check")
    op.create_check_constraint("ck_api_keys_role", "api_keys", "role IN ('admin', 'host')")
