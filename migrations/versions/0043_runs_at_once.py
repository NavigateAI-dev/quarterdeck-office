"""How many tasks the office runs at the same time (MW).

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Empty follows MAX_CONCURRENT_RUNS in the configuration (3 by default).
    op.add_column("office_settings", sa.Column("runs_at_once", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("office_settings", "runs_at_once")
