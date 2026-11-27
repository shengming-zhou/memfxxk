"""Allow source-grounded opinion records in the normal record lifecycle."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

PREVIOUS = "type IN ('turn', 'note', 'fact', 'summary', 'lesson', 'doc_chunk')"
CURRENT = PREVIOUS.replace("'doc_chunk'", "'doc_chunk', 'opinion'")


def upgrade() -> None:
    op.drop_constraint("ck_records_type", "records", type_="check")
    op.create_check_constraint("ck_records_type", "records", CURRENT)


def downgrade() -> None:
    table = sa.table("records", sa.column("type"))
    op.execute(table.delete().where(table.c.type == "opinion"))
    op.drop_constraint("ck_records_type", "records", type_="check")
    op.create_check_constraint("ck_records_type", "records", PREVIOUS)
