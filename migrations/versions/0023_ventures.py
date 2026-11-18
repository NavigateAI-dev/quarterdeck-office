"""Ventures: the business line a project's work is for, and where a lesson applies.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ventures",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("doctrine", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(64), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("uq_ventures_name", "ventures", ["name"], unique=True)
    op.create_index("ix_ventures_status", "ventures", ["status"])

    # Nullable: a project without a venture behaves exactly as it did before.
    op.add_column("projects", sa.Column("venture_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_projects_venture_id_ventures", "projects", "ventures", ["venture_id"], ["id"]
    )
    op.create_index("ix_projects_venture_id", "projects", ["venture_id"])

    op.add_column(
        "project_memory",
        sa.Column("scope", sa.String(16), nullable=False, server_default="project"),
    )
    op.add_column("project_memory", sa.Column("venture_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_index("ix_project_memory_venture_id", "project_memory", ["venture_id"])


def downgrade() -> None:
    op.drop_index("ix_project_memory_venture_id", table_name="project_memory")
    op.drop_column("project_memory", "venture_id")
    op.drop_column("project_memory", "scope")
    op.drop_index("ix_projects_venture_id", table_name="projects")
    op.drop_constraint("fk_projects_venture_id_ventures", "projects", type_="foreignkey")
    op.drop_column("projects", "venture_id")
    op.drop_table("ventures")
