"""When a calendar change was handed to the Mac helper, so it is handed out once.

Revision ID: 0077
Revises: 0076
"""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_changes", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("calendar_changes", "sent_at")
