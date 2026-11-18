"""Watching pages and topics.

Revision ID: 0065
Revises: 0064
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("condition", sa.Text(), nullable=False, server_default=""),
        sa.Column("every_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "post_to_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_destinations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("daily_cap_usd", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("stopped_reason", sa.String(200), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_digest", sa.String(64), nullable=True),
        sa.Column(
            "last_lines_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_said", sa.Text(), nullable=True),
        sa.Column("last_said_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_watches_status", "watches", ["status"])
    op.create_index("ix_watches_next_check_at", "watches", ["next_check_at"])
    op.create_table(
        "watch_checks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "watch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("watches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("said", sa.Text(), nullable=True),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
    )
    op.create_index("ix_watch_checks_watch_id", "watch_checks", ["watch_id"])
    op.create_index("ix_watch_checks_at", "watch_checks", ["at"])


def downgrade() -> None:
    op.drop_table("watch_checks")
    op.drop_table("watches")
