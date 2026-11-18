"""A delivery's buttons and files, for Slack threads that get the work.

Revision ID: 0059
Revises: 0058
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channel_deliveries", sa.Column("blocks_json", postgresql.JSONB(), nullable=True))
    op.add_column("channel_deliveries", sa.Column("files_json", postgresql.JSONB(), nullable=True))
    op.add_column("channel_deliveries", sa.Column("files_error", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_deliveries", "files_error")
    op.drop_column("channel_deliveries", "files_json")
    op.drop_column("channel_deliveries", "blocks_json")
