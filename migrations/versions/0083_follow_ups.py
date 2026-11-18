"""Follow-ups: what was due on a day, and where the owner says it stands.

Revision ID: 0083
Revises: 0082
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "follow_ups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(600), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("progress", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "notes_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("asked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_asked_on", sa.Date(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_follow_ups_status", "follow_ups", ["status"])


def downgrade() -> None:
    op.drop_index("ix_follow_ups_status", table_name="follow_ups")
    op.drop_table("follow_ups")
