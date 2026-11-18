"""Chat channels: app credentials (by reference), installations, destinations,
and a delivery ledger with idempotency keys.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


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
        "channel_apps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("client_id", sa.String(255)),
        sa.Column("client_secret_ref", sa.String(255)),
        sa.Column("signing_secret_ref", sa.String(255)),
        sa.Column("app_token_ref", sa.String(255)),
        sa.Column("redirect_uri", sa.Text()),
        *_stamps(),
    )
    op.create_index("uq_channel_apps_kind", "channel_apps", ["kind"], unique=True)
    op.create_table(
        "channel_installations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("workspace_name", sa.String(255), nullable=False),
        sa.Column("bot_user_id", sa.String(64), nullable=False),
        sa.Column("token_ref", sa.String(255), nullable=False),
        sa.Column("scopes_json", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("owner_external_id", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        *_stamps(),
    )
    op.create_index(
        "uq_channel_installations_kind_workspace",
        "channel_installations",
        ["kind", "workspace_id"],
        unique=True,
    )
    op.create_table(
        "channel_destinations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "installation_id",
            UUID,
            sa.ForeignKey(
                "channel_installations.id",
                name="fk_channel_destinations_installation_id_channel_installations",
            ),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("events_json", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_stamps(),
    )
    op.create_index(
        "uq_channel_destinations_installation_external",
        "channel_destinations",
        ["installation_id", "external_id"],
        unique=True,
    )
    op.create_table(
        "channel_deliveries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "destination_id",
            UUID,
            sa.ForeignKey(
                "channel_destinations.id",
                name="fk_channel_deliveries_destination_id_channel_destinations",
            ),
            nullable=False,
        ),
        sa.Column("run_id", UUID),
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("external_ref", sa.String(255)),
        sa.Column("error", sa.String(64)),
        *_stamps(),
    )
    op.create_index(
        "uq_channel_deliveries_idempotency_key",
        "channel_deliveries",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_channel_deliveries_run_id", "channel_deliveries", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_channel_deliveries_run_id", table_name="channel_deliveries")
    op.drop_index("uq_channel_deliveries_idempotency_key", table_name="channel_deliveries")
    op.drop_table("channel_deliveries")
    op.drop_index(
        "uq_channel_destinations_installation_external", table_name="channel_destinations"
    )
    op.drop_table("channel_destinations")
    op.drop_index("uq_channel_installations_kind_workspace", table_name="channel_installations")
    op.drop_table("channel_installations")
    op.drop_index("uq_channel_apps_kind", table_name="channel_apps")
    op.drop_table("channel_apps")
