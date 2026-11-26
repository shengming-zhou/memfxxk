"""Isolate project credential idempotency keys.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events", sa.Column("idempotency_namespace", sa.Text, nullable=False, server_default="")
    )
    op.create_unique_constraint(
        "uq_events_org_namespace_idempotency_key",
        "events",
        ["org_id", "idempotency_namespace", "idempotency_key"],
    )
    op.drop_constraint("uq_events_org_idempotency_key", "events", type_="unique")


def downgrade() -> None:
    # The old constraint cannot represent separately scoped keys. Retain the original events.
    op.execute(
        sa.text("UPDATE events SET idempotency_key = NULL WHERE idempotency_namespace <> ''")
    )
    op.create_unique_constraint(
        "uq_events_org_idempotency_key", "events", ["org_id", "idempotency_key"]
    )
    op.drop_constraint("uq_events_org_namespace_idempotency_key", "events", type_="unique")
    op.drop_column("events", "idempotency_namespace")
