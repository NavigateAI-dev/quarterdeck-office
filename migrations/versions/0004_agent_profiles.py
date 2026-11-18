"""Versioned agent profiles and immutable per-run configuration snapshots.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False, server_default="ACTIVE"),
        sa.Column("enabled_version", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("uq_agent_profiles_name", "agent_profiles", ["name"], unique=True)
    op.create_index("ix_agent_profiles_status", "agent_profiles", ["status"])
    op.create_table(
        "agent_profile_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "agent_profiles.id",
                name="fk_agent_profile_versions_profile_id_agent_profiles",
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "model_connections.id",
                name="fk_agent_profile_versions_connection_id_model_connections",
            ),
        ),
        sa.Column(
            "limits_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_agent_profile_versions_profile_version",
        "agent_profile_versions",
        ["profile_id", "version"],
        unique=True,
    )
    op.create_index(
        "ix_agent_profile_versions_profile_id", "agent_profile_versions", ["profile_id"]
    )
    op.create_index(
        "ix_agent_profile_versions_connection_id", "agent_profile_versions", ["connection_id"]
    )
    op.add_column(
        "runs",
        sa.Column(
            "config_snapshot_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "config_snapshot_json")
    op.drop_index("ix_agent_profile_versions_connection_id", table_name="agent_profile_versions")
    op.drop_index("ix_agent_profile_versions_profile_id", table_name="agent_profile_versions")
    op.drop_index("uq_agent_profile_versions_profile_version", table_name="agent_profile_versions")
    op.drop_table("agent_profile_versions")
    op.drop_index("ix_agent_profiles_status", table_name="agent_profiles")
    op.drop_index("uq_agent_profiles_name", table_name="agent_profiles")
    op.drop_table("agent_profiles")
