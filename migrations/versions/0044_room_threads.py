"""Threads in a project's room, as in Slack.

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The message a thread hangs from; empty for the main conversation.
    op.add_column("room_messages", sa.Column("thread_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_room_messages_thread_id", "room_messages", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_room_messages_thread_id", table_name="room_messages")
    op.drop_column("room_messages", "thread_id")
