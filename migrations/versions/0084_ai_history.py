"""AI history: the owner's conversations from other AI tools, as they were said.

Revision ID: 0084
Revises: 0083
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")

    def created_at() -> sa.Column:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    op.create_table(
        "archive_imports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column("conversations_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversations_seen", sa.Integer(), nullable=False, server_default="0"),
        created_at(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "archive_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(16), nullable=False, server_default="excluded"),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "flags_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "import_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("archive_imports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        created_at(),
        sa.UniqueConstraint("provider", "external_id", name="uq_archive_conversations_external"),
    )
    op.create_table(
        "archive_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("archive_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("conversation_id", "external_id", name="uq_archive_messages_external"),
    )
    op.create_index("ix_archive_messages_sent_at", "archive_messages", ["sent_at"])
    op.create_index(
        "ix_archive_messages_content_trgm",
        "archive_messages",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "public.gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_table("archive_messages")
    op.drop_table("archive_conversations")
    op.drop_table("archive_imports")
