"""A run's state as a projection of its log of changes.

Each run gets its state so far as a first change ("baseline"), so every run's
log adds up to its state from the start.

Revision ID: 0071
Revises: 0070
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_state_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("op", sa.String(8), nullable=False),
        sa.Column("patch_json", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_run_state_changes_run_id_id", "run_state_changes", ["run_id", "id"])
    op.execute(
        "INSERT INTO run_state_changes (run_id, op, patch_json, source) "
        "SELECT id, 'replace', COALESCE(state_json, '{}'::jsonb), 'baseline' FROM runs"
    )


def downgrade() -> None:
    op.drop_index("ix_run_state_changes_run_id_id", table_name="run_state_changes")
    op.drop_table("run_state_changes")
