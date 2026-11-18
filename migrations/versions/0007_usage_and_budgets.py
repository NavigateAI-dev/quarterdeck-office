"""Durable model usage per invocation and a budget per run.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "budget_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("runs.id", name="fk_usage_events_run_id_runs"),
            nullable=False,
        ),
        sa.Column("task_id", UUID),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_alias", sa.String(64), nullable=False),
        sa.Column("resolved_model", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("estimated_cost", sa.Float()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_events_run_id", "usage_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_run_id", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_column("runs", "budget_json")
