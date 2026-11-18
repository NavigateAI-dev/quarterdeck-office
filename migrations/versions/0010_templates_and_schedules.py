"""Reusable task templates and timezone-aware schedules.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _stamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "task_templates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("projects.id", name="fk_task_templates_project_id_projects"),
        ),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "budget_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("source_run_id", UUID),
        *_stamps(),
    )
    op.create_index("uq_task_templates_name", "task_templates", ["name"], unique=True)
    op.create_table(
        "schedules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "template_id",
            UUID,
            sa.ForeignKey("task_templates.id", name="fk_schedules_template_id_task_templates"),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("local_time", sa.String(5), nullable=False),
        sa.Column("weekday", sa.Integer()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_run_id", UUID),
        sa.Column("last_outcome", sa.String(64)),
        sa.Column("disabled_reason", sa.String(64)),
        *_stamps(),
    )
    op.create_index("ix_schedules_template_id", "schedules", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_schedules_template_id", table_name="schedules")
    op.drop_table("schedules")
    op.drop_index("uq_task_templates_name", table_name="task_templates")
    op.drop_table("task_templates")
