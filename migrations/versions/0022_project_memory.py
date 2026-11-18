"""Project memory: lessons a finished run produced, pending the owner's word.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_memory",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("lesson", sa.Text(), nullable=False),
        sa.Column("lesson_digest", sa.String(64), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="CANDIDATE"),
        sa.Column("reusable_probability", sa.Float(), nullable=True),
        sa.Column("evaluator", sa.String(64), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_project_memory_project_id_projects"
        ),
    )
    op.create_index("ix_project_memory_project_id", "project_memory", ["project_id"])
    op.create_index("ix_project_memory_run_id", "project_memory", ["run_id"])
    op.create_index("ix_project_memory_status", "project_memory", ["status"])
    # One lesson per project, however many runs rediscover it.
    op.create_index(
        "uq_project_memory_lesson", "project_memory", ["project_id", "lesson_digest"], unique=True
    )


def downgrade() -> None:
    op.drop_table("project_memory")
