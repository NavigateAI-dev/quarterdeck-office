"""Where a scheduled task posts its result.

Revision ID: 0060
Revises: 0059
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column(
            "post_to_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_destinations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("schedules", "post_to_id")
