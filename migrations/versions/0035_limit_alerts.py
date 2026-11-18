"""Warnings at 80% and 100% of a limit, recorded once per crossing.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "limit_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # office, project or task
        sa.Column("scope", sa.String(16), nullable=False),
        # The project's or task's id; "office" for the office.
        sa.Column("subject", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        # The month ("2026-09") for a monthly limit; "run" for a task's budget.
        sa.Column("window", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("share", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_limit_alerts_crossing",
        "limit_alerts",
        ["scope", "subject", "window", "threshold"],
        unique=True,
    )
    op.create_index("ix_limit_alerts_created_at", "limit_alerts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_limit_alerts_created_at", table_name="limit_alerts")
    op.drop_index("uq_limit_alerts_crossing", table_name="limit_alerts")
    op.drop_table("limit_alerts")
