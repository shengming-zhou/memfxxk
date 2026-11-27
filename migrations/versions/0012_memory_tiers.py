"""Store scoped MemoryOS segment summaries and turn membership."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tier_segments",
        sa.Column("id", sa.UUID, primary_key=True),
        sa.Column("org_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text),
        sa.Column("agent_id", sa.Text),
        sa.Column("project_id", sa.Text),
        sa.Column("bank_id", sa.UUID, nullable=False),
        sa.Column("embedding", VECTOR, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("keywords", ARRAY(sa.Text), nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("source_event_ids", ARRAY(sa.UUID), nullable=False, server_default="{}"),
        sa.Column("generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("summarized_generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("visits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unpromoted_pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("promotion_epoch", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_access", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tier_segments_bank", "tier_segments", ["org_id", "bank_id"])
    op.create_index(
        "ix_tier_segments_sources", "tier_segments", ["source_event_ids"], postgresql_using="gin"
    )
    op.create_table(
        "tier_pages",
        sa.Column(
            "record_id",
            sa.UUID,
            sa.ForeignKey("records.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("bank_id", sa.UUID, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("segment_id", sa.UUID, sa.ForeignKey("tier_segments.id", ondelete="SET NULL")),
    )
    op.create_index("ix_tier_pages_bank_state", "tier_pages", ["bank_id", "state"])
    op.create_index("ix_tier_pages_segment", "tier_pages", ["segment_id"])


def downgrade() -> None:
    op.drop_table("tier_pages")
    op.drop_table("tier_segments")
