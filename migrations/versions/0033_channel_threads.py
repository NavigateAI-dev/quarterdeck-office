"""What a chat thread remembers, so a restart does not forget it.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_threads",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("app_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("thread_key", sa.String(64), nullable=False),
        sa.Column("proposal_json", JSONB(), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("turns_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index(
        "uq_channel_threads_key",
        "channel_threads",
        ["app_id", "channel_id", "thread_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_channel_threads_key", table_name="channel_threads")
    op.drop_table("channel_threads")
