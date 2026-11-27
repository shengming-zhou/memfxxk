"""Record strategy and prompt provenance on derived projections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

TABLES = ("tier_segments", "block_revisions", "block_proposals", "entity_edges", "amem_revisions")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("metadata", JSONB, nullable=False, server_default="{}"))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_column(table, "metadata")
