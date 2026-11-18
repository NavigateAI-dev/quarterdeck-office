"""The owner may let a server's tools reach discussions with no project.

Off for every server: nothing widens until the owner ticks it.

Revision ID: 0080
Revises: 0079
"""

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("no_project_discussions", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "no_project_discussions")
