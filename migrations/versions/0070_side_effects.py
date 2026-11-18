"""Writes that leave the office, checkpointed before they run.

Revision ID: 0070
Revises: 0069
"""

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "side_effects",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("settled_by", sa.String(128), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("run_id", "key", name="uq_side_effects_run_key"),
    )
    op.create_index("ix_side_effects_run_id", "side_effects", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_side_effects_run_id", table_name="side_effects")
    op.drop_table("side_effects")
