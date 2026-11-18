"""One approval is carried out once: a record of each engineering
execution, written before it starts and completed with its report, so recovery
can tell work not yet begun from work done or interrupted.

Revision ID: 0050
Revises: 0049
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engineering_executions",
        # An approval authorizes exactly one execution.
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approvals.id"),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        # STARTED until the report is recorded; FINISHED after.
        sa.Column("status", sa.String(16), nullable=False, server_default="STARTED"),
        sa.Column("report_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("engineering_executions")
