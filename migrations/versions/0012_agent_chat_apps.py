"""Several chat apps (one per agent bot), members per workspace, and deliveries
tagged with the app and agent that sent them.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # Apps: the office app has no agent; each agent may have one app per kind.
    op.add_column(
        "channel_apps",
        sa.Column(
            "agent_profile_id",
            UUID,
            sa.ForeignKey(
                "agent_profiles.id", name="fk_channel_apps_agent_profile_id_agent_profiles"
            ),
        ),
    )
    op.drop_index("uq_channel_apps_kind", table_name="channel_apps")
    op.create_index(
        "uq_channel_apps_office_kind",
        "channel_apps",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("agent_profile_id IS NULL"),
    )
    op.create_index(
        "uq_channel_apps_agent_kind",
        "channel_apps",
        ["kind", "agent_profile_id"],
        unique=True,
        postgresql_where=sa.text("agent_profile_id IS NOT NULL"),
    )

    # Installations belong to an app. Existing ones belong to the office app,
    # which is created if a bot token was installed before any app settings.
    op.add_column(
        "channel_installations",
        sa.Column(
            "app_id",
            UUID,
            sa.ForeignKey("channel_apps.id", name="fk_channel_installations_app_id_channel_apps"),
        ),
    )
    op.execute(
        """
        INSERT INTO channel_apps (id, kind)
        SELECT gen_random_uuid(), i.kind FROM channel_installations i
        WHERE NOT EXISTS (
            SELECT 1 FROM channel_apps a WHERE a.kind = i.kind AND a.agent_profile_id IS NULL
        )
        GROUP BY i.kind
        """
    )
    op.execute(
        """
        UPDATE channel_installations i SET app_id = a.id FROM channel_apps a
        WHERE a.kind = i.kind AND a.agent_profile_id IS NULL
        """
    )
    op.alter_column("channel_installations", "app_id", nullable=False)
    op.drop_index("uq_channel_installations_kind_workspace", table_name="channel_installations")
    op.create_index(
        "uq_channel_installations_app_workspace",
        "channel_installations",
        ["app_id", "workspace_id"],
        unique=True,
    )

    # People who may talk to the bots of a workspace. V1: the owner only.
    op.create_table(
        "channel_members",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("external_user_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("daily_tokens", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_channel_members_workspace_user",
        "channel_members",
        ["kind", "workspace_id", "external_user_id"],
        unique=True,
    )
    op.create_index(
        "uq_channel_members_one_owner",
        "channel_members",
        ["kind", "workspace_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
    )
    op.execute(
        """
        INSERT INTO channel_members (id, kind, workspace_id, external_user_id, role)
        SELECT DISTINCT ON (kind, workspace_id)
            gen_random_uuid(), kind, workspace_id, owner_external_id, 'owner'
        FROM channel_installations WHERE owner_external_id IS NOT NULL
        """
    )
    op.drop_column("channel_installations", "owner_external_id")

    # Chat model usage per bot and day: the durable daily allowance, and the
    # owner's view of what conversation costs.
    op.create_table(
        "channel_usage",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "app_id",
            UUID,
            sa.ForeignKey("channel_apps.id", name="fk_channel_usage_app_id_channel_apps"),
            nullable=False,
        ),
        sa.Column("agent_profile_id", UUID),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Float()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("uq_channel_usage_app_day", "channel_usage", ["app_id", "day"], unique=True)

    # The office keeps the record of every delivery, whichever bot sent it.
    op.add_column("channel_deliveries", sa.Column("app_id", UUID))
    op.add_column("channel_deliveries", sa.Column("agent_profile_id", UUID))
    # A reply in the chat thread the work was requested from.
    op.add_column("channel_deliveries", sa.Column("thread_ref", sa.String(64)))


def downgrade() -> None:
    op.drop_table("channel_usage")
    op.drop_column("channel_deliveries", "thread_ref")
    op.drop_column("channel_deliveries", "agent_profile_id")
    op.drop_column("channel_deliveries", "app_id")
    op.add_column("channel_installations", sa.Column("owner_external_id", sa.String(64)))
    op.execute(
        """
        UPDATE channel_installations i SET owner_external_id = m.external_user_id
        FROM channel_members m
        WHERE m.kind = i.kind AND m.workspace_id = i.workspace_id AND m.role = 'owner'
        """
    )
    op.drop_table("channel_members")
    # Agent installations and apps cannot exist under the old schema.
    op.execute(
        "DELETE FROM channel_destinations WHERE installation_id IN (SELECT i.id FROM "
        "channel_installations i JOIN channel_apps a ON a.id = i.app_id "
        "WHERE a.agent_profile_id IS NOT NULL)"
    )
    op.execute(
        "DELETE FROM channel_installations WHERE app_id IN "
        "(SELECT id FROM channel_apps WHERE agent_profile_id IS NOT NULL)"
    )
    op.drop_index("uq_channel_installations_app_workspace", table_name="channel_installations")
    op.create_index(
        "uq_channel_installations_kind_workspace",
        "channel_installations",
        ["kind", "workspace_id"],
        unique=True,
    )
    op.drop_column("channel_installations", "app_id")
    op.execute("DELETE FROM channel_apps WHERE agent_profile_id IS NOT NULL")
    op.drop_index("uq_channel_apps_agent_kind", table_name="channel_apps")
    op.drop_index("uq_channel_apps_office_kind", table_name="channel_apps")
    op.create_index("uq_channel_apps_kind", "channel_apps", ["kind"], unique=True)
    op.drop_column("channel_apps", "agent_profile_id")
