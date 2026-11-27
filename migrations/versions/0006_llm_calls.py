"""Add a scoped audit and token reservation ledger for memory model calls."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.UUID, primary_key=True),
        sa.Column("org_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text),
        sa.Column("agent_id", sa.Text),
        sa.Column("project_id", sa.Text),
        sa.Column("task", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("request_id", sa.UUID),
        sa.Column("job_id", sa.UUID),
        sa.Column("input_sha256", sa.Text, nullable=False),
        sa.Column(
            "source_event_ids", ARRAY(sa.UUID), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("retries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("debug_input", JSONB),
        sa.Column("debug_output", JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'budget_exceeded')",
            name="ck_llm_calls_status",
        ),
    )
    op.create_index("ix_llm_calls_org_day", "llm_calls", ["org_id", "created_at"])
    op.create_index(
        "ix_llm_calls_sources", "llm_calls", ["source_event_ids"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_table("llm_calls")
