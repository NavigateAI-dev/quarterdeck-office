"""Tools the office does not hard-code: MCP servers and what they offer.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("url", sa.Text(), nullable=False),
        # A reference into the secret store, never a value.
        sa.Column("credential_ref", sa.String(255)),
        # UNREACHED until a handshake succeeds, then REACHABLE or UNREACHABLE.
        sa.Column("status", sa.String(32), nullable=False, server_default="UNREACHED"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("server_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("server_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("protocol_version", sa.String(32), nullable=False, server_default=""),
        # What it offered when last asked, and a digest of that list: a server
        # that changes what it offers must be reviewed again.
        sa.Column("offering_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("offering_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("listed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
