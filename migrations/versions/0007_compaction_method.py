"""Persist the checkpoint method, citations, and exact retained-event references."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compactions", sa.Column("method", sa.Text, nullable=False, server_default="legacy")
    )
    op.add_column("compactions", sa.Column("checkpoint", JSONB))
    op.add_column(
        "compactions",
        sa.Column(
            "retained_user_event_ids",
            ARRAY(sa.UUID),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("compactions", "retained_user_event_ids")
    op.drop_column("compactions", "checkpoint")
    op.drop_column("compactions", "method")
