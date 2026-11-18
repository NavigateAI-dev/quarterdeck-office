"""How AI history is protected, by the owner's choice, and who reached it.

Revision ID: 0086
Revises: 0085
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "office_settings",
        sa.Column(
            "archive_protection_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "archive_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("who", sa.String(255), nullable=False),
        sa.Column("place", sa.String(16), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_archive_access_created_at", "archive_access", ["created_at"])


def downgrade() -> None:
    op.drop_table("archive_access")
    op.drop_column("office_settings", "archive_protection_json")
