"""Limit warnings announced through chat, once each.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "limit_alerts", sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Warnings from before this revision were seen in the office already; they
    # are not announced now, days late.
    op.execute("UPDATE limit_alerts SET notified_at = now()")
    # Every channel already told about the office hears about its limits too;
    # the owner can untick it.
    op.execute(
        """
        UPDATE channel_destinations
        SET events_json = events_json || '["limit_warning"]'::jsonb
        WHERE enabled AND NOT (events_json ? 'limit_warning')
        """
    )


def downgrade() -> None:
    op.execute("UPDATE channel_destinations SET events_json = events_json - 'limit_warning'")
    op.drop_column("limit_alerts", "notified_at")
