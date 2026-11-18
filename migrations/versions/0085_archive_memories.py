"""What was worth keeping from AI history: proposed from its messages, decided by the owner.

Revision ID: 0085
Revises: 0084
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def created_at() -> sa.Column:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    op.create_table(
        "archive_readings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("archive_conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("messages_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proposed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dropped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        created_at(),
    )
    op.create_index("ix_archive_readings_created_at", "archive_readings", ["created_at"])
    op.create_table(
        "archive_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("archive_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("said_by", sa.String(8), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("corrected_from", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        created_at(),
        sa.UniqueConstraint("conversation_id", "digest", name="uq_archive_memories_digest"),
    )
    op.create_index("ix_archive_memories_status", "archive_memories", ["status"])


def downgrade() -> None:
    op.drop_table("archive_memories")
    op.drop_table("archive_readings")
