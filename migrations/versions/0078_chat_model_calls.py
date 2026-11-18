"""Model calls behind discussion replies and Slack, kept as a task's are.

A call belongs to a run, or to the discussion message it answered (deleted with
it), or to a Slack thread (kept for a while, then let go).

Revision ID: 0078
Revises: 0077
"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("model_calls", "run_id", nullable=True)
    op.add_column(
        "model_calls",
        sa.Column("origin", sa.String(16), nullable=False, server_default="task"),
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "topic_id", sa.Uuid(), sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=True
        ),
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("room_messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column("model_calls", sa.Column("chat_ref", sa.String(160), nullable=True))
    op.create_index("ix_model_calls_message_id", "model_calls", ["message_id"])
    op.create_index("ix_model_calls_topic_id", "model_calls", ["topic_id"])
    op.create_index("ix_model_calls_origin_created", "model_calls", ["origin", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_model_calls_origin_created", table_name="model_calls")
    op.drop_index("ix_model_calls_topic_id", table_name="model_calls")
    op.drop_index("ix_model_calls_message_id", table_name="model_calls")
    op.execute("DELETE FROM model_calls WHERE run_id IS NULL")
    op.drop_column("model_calls", "chat_ref")
    op.drop_column("model_calls", "message_id")
    op.drop_column("model_calls", "topic_id")
    op.drop_column("model_calls", "origin")
    op.alter_column("model_calls", "run_id", nullable=False)
