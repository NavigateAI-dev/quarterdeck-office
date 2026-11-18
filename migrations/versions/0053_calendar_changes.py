"""The owner edits their calendar: each event's calendar id, and the queue of
changes the Mac helper applies.

Revision ID: 0053
Revises: 0052
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column("calendar_id", sa.String(255), nullable=False, server_default=""),
    )
    op.create_table(
        "calendar_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calendar_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_calendar_changes_source_id", "calendar_changes", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_calendar_changes_source_id", table_name="calendar_changes")
    op.drop_table("calendar_changes")
    op.drop_column("calendar_events", "calendar_id")
