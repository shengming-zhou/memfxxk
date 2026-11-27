"""Add versioned lore keywords and explicit in-world time intervals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_revisions", sa.Column("lore", JSONB(), nullable=False, server_default="{}")
    )
    op.create_table(
        "story_annotations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("agent_id", sa.Text()),
        sa.Column("project_id", sa.Text()),
        sa.Column("fact_id", sa.UUID(), sa.ForeignKey("records.id", ondelete="CASCADE")),
        sa.Column("edge_id", sa.UUID(), sa.ForeignKey("entity_edges.id", ondelete="CASCADE")),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("start", sa.BigInteger(), nullable=False),
        sa.Column("end", sa.BigInteger()),
        sa.Column(
            "source_event_id",
            sa.UUID(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "num_nonnulls(fact_id, edge_id) = 1", name="ck_story_annotations_target"
        ),
        sa.CheckConstraint('"end" > start', name="ck_story_annotations_interval"),
    )
    for kind in ("fact", "edge"):
        op.create_index(f"ix_story_annotations_{kind}", "story_annotations", [f"{kind}_id"])


def downgrade() -> None:
    op.drop_table("story_annotations")
    op.drop_column("document_revisions", "lore")
