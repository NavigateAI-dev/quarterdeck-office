"""A watch started from Slack answers in the thread it was asked in.

Revision ID: 0066
Revises: 0065
"""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watches", sa.Column("post_thread", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("watches", "post_thread")
