"""Folders of the owner's, given to a business space.

Revision ID: 0073
Revises: 0072
"""

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("venture_id", sa.Uuid(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("access", sa.String(8), nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=True),
        sa.Column("granted_by", sa.String(128), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("venture_id", "path", name="uq_workspaces_space_path"),
    )
    op.create_index("ix_workspaces_venture_id", "workspaces", ["venture_id"])
    op.create_table(
        "workspace_grants",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("venture_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("access", sa.String(8), nullable=False),
        sa.Column("by", sa.String(128), nullable=False),
        # What it was about, when more than the folder: the branch merged (6d).
        sa.Column("detail", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_workspace_grants_venture_id", "workspace_grants", ["venture_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_grants_venture_id", table_name="workspace_grants")
    op.drop_table("workspace_grants")
    op.drop_index("ix_workspaces_venture_id", table_name="workspaces")
    op.drop_table("workspaces")
