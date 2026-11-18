"""Which projects may use which MCP servers.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_mcp_servers",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "project_id", PG_UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "server_id", PG_UUID(as_uuid=True), sa.ForeignKey("mcp_servers.id"), nullable=False
        ),
    )
    op.create_index(
        "uq_project_mcp_servers", "project_mcp_servers", ["project_id", "server_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_project_mcp_servers", table_name="project_mcp_servers")
    op.drop_table("project_mcp_servers")
