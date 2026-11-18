"""The owner's decision on each tool a server offers.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keyed by tool name, each holding the owner's stance and a digest of the
    # tool as it stood when they decided: a tool that changes afterwards is
    # unreviewed again rather than carrying a decision made about something else.
    op.add_column(
        "mcp_servers",
        sa.Column("review_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "review_json")
