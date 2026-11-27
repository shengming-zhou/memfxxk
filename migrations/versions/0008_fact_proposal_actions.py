"""Record reviewable reconciliation actions and the version they apply to."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fact_proposals", sa.Column("operation", sa.Text, nullable=False, server_default="propose")
    )
    op.add_column("fact_proposals", sa.Column("base_version", sa.Integer))
    op.execute(
        sa.text("""
        UPDATE fact_proposals
           SET operation = fact #>> '{mem0_review,operation}',
               base_version = (fact #>> '{mem0_review,base_version}')::integer,
               status = fact #>> '{mem0_review,status}',
               fact = fact - 'mem0_review'
         WHERE fact ? 'mem0_review'
    """)
    )
    op.create_check_constraint(
        "ck_fact_proposal_operation",
        "fact_proposals",
        "(operation = 'propose' AND base_version IS NULL) "
        "OR (operation IN ('update', 'delete') AND base_version > 0 AND base_version IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_fact_proposal_operation", "fact_proposals", type_="check")
    op.execute(
        sa.text("""
        UPDATE fact_proposals
           SET fact = fact || jsonb_build_object(
                   'mem0_review', jsonb_build_object(
                      'operation', operation, 'base_version', base_version, 'status', status
                   )
               ),
               status = CASE WHEN status = 'pending' THEN 'rejected' ELSE status END
         WHERE operation <> 'propose'
    """)
    )
    op.drop_column("fact_proposals", "base_version")
    op.drop_column("fact_proposals", "operation")
