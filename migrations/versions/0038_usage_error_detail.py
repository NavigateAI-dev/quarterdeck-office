"""Why a call's attempts were rejected, kept with its usage record.

Revision ID: 0038
Revises: 0037
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per attempt, in words: never the model's output.
    op.add_column("usage_events", sa.Column("error_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_events", "error_detail")
