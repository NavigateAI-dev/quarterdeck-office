"""The owner's verdict on an answer: a discussion reply or a task's (IMPLEMENTATION_PLAN 3.0).

Revision ID: 0076
Revises: 0075
"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_verdicts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "room_message_id",
            sa.BigInteger(),
            sa.ForeignKey("room_messages.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        # The full-team redo it led to, when the owner asked for one.
        sa.Column("redo_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(room_message_id IS NULL) <> (run_id IS NULL)", name="ck_answer_verdicts_one_target"
        ),
    )
    op.create_index(
        "uq_answer_verdicts_run",
        "answer_verdicts",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("room_message_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_answer_verdicts_run", table_name="answer_verdicts")
    op.drop_table("answer_verdicts")
