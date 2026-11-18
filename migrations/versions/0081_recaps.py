"""A schedule can be a recap: run by the lead, reported to a discussion.

Revision ID: 0081
Revises: 0080
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column("workflow", sa.String(16), nullable=False, server_default="v0_analysis"),
    )
    op.add_column(
        "schedules",
        sa.Column(
            "topic_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("topics.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("schedules", sa.Column("recap", sa.String(16), nullable=True))
    op.create_index("uq_schedules_recap", "schedules", ["recap"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_schedules_recap", table_name="schedules")
    op.drop_column("schedules", "recap")
    op.drop_column("schedules", "topic_id")
    op.drop_column("schedules", "workflow")
