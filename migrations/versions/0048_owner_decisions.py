"""Decisions only from the owner, with their source and history; related work
kept within its business line.

Revision ID: 0048
Revises: 0047
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Where the owner said it, and what became of it: in force, or replaced.
    op.add_column("decisions", sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("decisions", sa.Column("source_message_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "decisions", sa.Column("status", sa.String(16), nullable=False, server_default="active")
    )
    op.add_column(
        "decisions", sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("decisions", sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True))
    # Related work from another business line only when the owner allows it.
    op.add_column(
        "office_settings",
        sa.Column("related_across_lines", sa.Boolean(), nullable=True, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("office_settings", "related_across_lines")
    op.drop_column("decisions", "replaced_at")
    op.drop_column("decisions", "replaced_by_id")
    op.drop_column("decisions", "status")
    op.drop_column("decisions", "source_message_id")
    op.drop_column("decisions", "topic_id")
