"""Store source-grounded summary and line variants for current content."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_resolutions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("agent_id", sa.Text()),
        sa.Column("project_id", sa.Text()),
        sa.Column("record_id", sa.UUID(), sa.ForeignKey("records.id", ondelete="CASCADE")),
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("block_id", sa.UUID(), sa.ForeignKey("memory_blocks.id", ondelete="CASCADE")),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("variants", JSONB(), nullable=False),
        sa.Column("source_event_ids", ARRAY(sa.UUID()), nullable=False),
        sa.CheckConstraint(
            "num_nonnulls(record_id, document_id, block_id) = 1",
            name="ck_content_resolutions_source",
        ),
    )
    op.create_index(
        "ix_content_resolutions_sources",
        "content_resolutions",
        ["source_event_ids"],
        postgresql_using="gin",
    )
    for key in ("record_id", "document_id", "block_id"):
        op.create_index(f"ix_content_resolutions_{key}", "content_resolutions", [key])


def downgrade() -> None:
    op.drop_table("content_resolutions")
