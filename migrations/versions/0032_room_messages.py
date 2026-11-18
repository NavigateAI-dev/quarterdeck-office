"""What is said in a project's room.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "room_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "project_id", PG_UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_role", sa.String(64), nullable=True),
        sa.Column("author_name", sa.String(128), nullable=False),
        sa.Column("author_profile_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_to", sa.BigInteger(), nullable=True),
        sa.Column("run_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("actions_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index("ix_room_messages_project_id", "room_messages", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_room_messages_project_id", table_name="room_messages")
    op.drop_table("room_messages")
