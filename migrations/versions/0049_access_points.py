"""Opening the office from a phone: the outside addresses the owner set
up, what the office verified about each, and when a phone was confirmed; and
a request id on what the owner says, so a resend is not a second message.

Revision ID: 0049
Revises: 0048
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_points",
        # home | remote
        sa.Column("kind", sa.String(16), primary_key=True),
        # The origin a phone opens, e.g. http://192.168.1.20:8000.
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Set only by a check that passed; cleared when the address changes.
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "check_json",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # A phone signed in through it, not merely shown a QR code.
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_device", sa.String(120), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("room_messages", sa.Column("client_key", sa.String(64), nullable=True))
    op.create_index(
        "uq_room_messages_client_key",
        "room_messages",
        ["topic_id", "client_key"],
        unique=True,
        postgresql_where=sa.text("client_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_room_messages_client_key", table_name="room_messages")
    op.drop_column("room_messages", "client_key")
    op.drop_table("access_points")
