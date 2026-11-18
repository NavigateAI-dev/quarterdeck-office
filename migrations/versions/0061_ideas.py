"""Ideas of their own, to plan for a day.

Ideas noted in a discussion move here from `topic_items`, keeping their ids,
so a link to one still finds it.

Revision ID: 0061
Revises: 0060
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ideas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="floor"),
        sa.Column(
            "topic_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("topics.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("plan_for", sa.Date(), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_ideas_topic_id", "ideas", ["topic_id"])
    op.create_index("ix_ideas_status", "ideas", ["status"])
    op.execute(
        """
        INSERT INTO ideas (id, text, source, topic_id, source_message_id, status,
                           created_at, updated_at)
        SELECT id, text, 'discussion', topic_id, source_message_id, status,
               created_at, updated_at
        FROM topic_items WHERE kind = 'idea'
        """
    )
    op.execute("DELETE FROM topic_items WHERE kind = 'idea'")


def downgrade() -> None:
    # Only ideas that belong to a discussion can go back; floor ideas have none.
    op.execute(
        """
        INSERT INTO topic_items (id, topic_id, kind, text, source_message_id, status,
                                 created_at, updated_at)
        SELECT id, topic_id, 'idea', text, source_message_id,
               CASE WHEN status IN ('open', 'taken', 'dropped') THEN status ELSE 'open' END,
               created_at, updated_at
        FROM ideas WHERE topic_id IS NOT NULL
        """
    )
    op.drop_index("ix_ideas_status", table_name="ideas")
    op.drop_index("ix_ideas_topic_id", table_name="ideas")
    op.drop_table("ideas")
