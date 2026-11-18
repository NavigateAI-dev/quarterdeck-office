"""A task can continue another, so a thread can be read as a thread.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately not a foreign key with a cascade: deleting a parent must not
    # silently take its follow-ups, which are work in their own right.
    op.add_column("runs", sa.Column("continues_run_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_index("ix_runs_continues_run_id", "runs", ["continues_run_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_continues_run_id", table_name="runs")
    op.drop_column("runs", "continues_run_id")
