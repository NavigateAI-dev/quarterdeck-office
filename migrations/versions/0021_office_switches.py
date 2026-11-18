"""Switches the owner can reach: pause new work, schedules, or chat.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("pause_new_tasks", "pause_schedules", "pause_chat"):
        op.add_column(
            "office_settings",
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    for column in ("pause_chat", "pause_schedules", "pause_new_tasks"):
        op.drop_column("office_settings", column)
