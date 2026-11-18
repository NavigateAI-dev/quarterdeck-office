"""Who answers a task-box request: the lead or the full team.

Revision ID: 0068
Revises: 0067
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("office_settings", sa.Column("answering", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("office_settings", "answering")
