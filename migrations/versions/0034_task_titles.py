"""A task can carry the title its owner gave it.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Empty for every task so far: they keep the title derived from their ask.
    op.add_column("tasks", sa.Column("title", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "title")
