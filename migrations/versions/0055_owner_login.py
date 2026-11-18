"""Signing in with a name and password the owner set, and devices kept
signed in.

Revision ID: 0055
Revises: 0054
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "office_settings",
        sa.Column(
            "owner_login_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "owner_sessions",
        sa.Column("remember", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("owner_sessions", "remember")
    op.drop_column("office_settings", "owner_login_json")
