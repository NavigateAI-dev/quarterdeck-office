"""Persist owner-scoped model connection metadata without provider secrets.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("credential_ref", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False, server_default="CONFIGURED"),
        sa.Column(
            "capabilities_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_model_connections_provider", "model_connections", ["provider"])
    op.create_index("ix_model_connections_status", "model_connections", ["status"])
    op.create_index(
        "uq_model_connections_provider_name",
        "model_connections",
        ["provider", "display_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_model_connections_provider_name", table_name="model_connections")
    op.drop_index("ix_model_connections_status", table_name="model_connections")
    op.drop_index("ix_model_connections_provider", table_name="model_connections")
    op.drop_table("model_connections")
