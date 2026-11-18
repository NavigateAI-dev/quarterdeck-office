"""The owner's calendars, connected read-only, and what the agents may see of
them.

Revision ID: 0052
Revises: 0051
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "office_settings",
        sa.Column(
            "calendar_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "calendar_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("server_url", sa.Text(), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("secret_name", sa.String(128), nullable=False),
        sa.Column(
            "calendars_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calendar_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uid", sa.String(512), nullable=False),
        sa.Column("calendar", sa.String(255), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("busy", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("location", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("attendees", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("source_id", "uid", name="uq_calendar_events_source_uid"),
    )
    op.create_index("ix_calendar_events_source_id", "calendar_events", ["source_id"])
    op.create_index("ix_calendar_events_time", "calendar_events", ["starts_at", "ends_at"])


def downgrade() -> None:
    op.drop_index("ix_calendar_events_time", table_name="calendar_events")
    op.drop_index("ix_calendar_events_source_id", table_name="calendar_events")
    op.drop_table("calendar_events")
    op.drop_table("calendar_sources")
    op.drop_column("office_settings", "calendar_json")
