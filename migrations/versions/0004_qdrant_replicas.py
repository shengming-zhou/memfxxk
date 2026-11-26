"""Track Qdrant projections through asynchronous hard deletion.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The placement outlives its PostgreSQL record until remote removal is acknowledged.
    op.create_table(
        "qdrant_replicas",
        sa.Column("id", sa.UUID, primary_key=True),
        sa.Column("record_id", sa.UUID, nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("collection", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("dim", sa.Integer, nullable=False),
        sa.Column("generation", sa.Integer),
        sa.UniqueConstraint(
            "record_id", "endpoint", "collection", name="uq_qdrant_replica_placement"
        ),
        sa.CheckConstraint("dim > 0", name="ck_qdrant_replicas_dim"),
    )
    op.create_index("ix_qdrant_replicas_endpoint", "qdrant_replicas", ["endpoint"])


def downgrade() -> None:
    op.execute("LOCK TABLE qdrant_replicas IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qdrant_replicas) THEN
                RAISE EXCEPTION 'Remove Qdrant collections and clear the replica registry '
                                'before downgrading from 0004';
            END IF;
        END
        $$
        """
    )
    op.execute("DELETE FROM jobs WHERE kind = 'sync_qdrant'")
    op.drop_table("qdrant_replicas")
