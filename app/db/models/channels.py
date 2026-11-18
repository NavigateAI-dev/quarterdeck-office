"""Tables for chat channels such as Slack: apps, members, deliveries."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, CreatedAt, Identity, UpdatedAt


class ChannelApp(Identity, CreatedAt, UpdatedAt, Base):
    """A chat platform app's configuration. Secrets are secret-store references."""

    __tablename__ = "channel_apps"
    __table_args__ = (
        Index(
            "uq_channel_apps_office_kind",
            "kind",
            unique=True,
            postgresql_where=text("agent_profile_id IS NULL"),
        ),
        Index(
            "uq_channel_apps_agent_kind",
            "kind",
            "agent_profile_id",
            unique=True,
            postgresql_where=text("agent_profile_id IS NOT NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(32))
    # None: the office's own bot. Otherwise the agent this bot speaks for.
    agent_profile_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_profiles.id"))
    client_id: Mapped[str | None] = mapped_column(String(255))
    client_secret_ref: Mapped[str | None] = mapped_column(String(255))
    signing_secret_ref: Mapped[str | None] = mapped_column(String(255))
    app_token_ref: Mapped[str | None] = mapped_column(String(255))
    redirect_uri: Mapped[str | None] = mapped_column(Text)


class ChannelInstallation(Identity, CreatedAt, UpdatedAt, Base):
    """The app installed in one workspace; its bot token lives in the secret store."""

    __tablename__ = "channel_installations"
    __table_args__ = (
        Index("uq_channel_installations_app_workspace", "app_id", "workspace_id", unique=True),
    )

    app_id: Mapped[UUID] = mapped_column(ForeignKey("channel_apps.id"))
    kind: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[str] = mapped_column(String(64))
    workspace_name: Mapped[str] = mapped_column(String(255))
    bot_user_id: Mapped[str] = mapped_column(String(64))
    token_ref: Mapped[str] = mapped_column(String(255))
    scopes_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(32), server_default="ACTIVE")


class ChannelMember(Identity, CreatedAt, UpdatedAt, Base):
    """A person who may talk to the office's bots in one workspace.

    V1 has one `owner` per workspace, set by pairing and trusted by every bot
    there. `member` rows (later) may talk within their own daily limit and can
    never approve anything.
    """

    __tablename__ = "channel_members"
    __table_args__ = (
        Index(
            "uq_channel_members_workspace_user",
            "kind",
            "workspace_id",
            "external_user_id",
            unique=True,
        ),
        Index(
            "uq_channel_members_one_owner",
            "kind",
            "workspace_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[str] = mapped_column(String(64))
    external_user_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    daily_tokens: Mapped[int | None] = mapped_column(Integer)


class ChannelDestination(Identity, CreatedAt, UpdatedAt, Base):
    """A channel the owner chose to receive selected notifications."""

    __tablename__ = "channel_destinations"
    __table_args__ = (
        Index(
            "uq_channel_destinations_installation_external",
            "installation_id",
            "external_id",
            unique=True,
        ),
    )

    installation_id: Mapped[UUID] = mapped_column(ForeignKey("channel_installations.id"))
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    events_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class ChannelUsage(Identity, CreatedAt, UpdatedAt, Base):
    """Chat model usage of one bot on one day (its daily allowance)."""

    __tablename__ = "channel_usage"
    __table_args__ = (Index("uq_channel_usage_app_day", "app_id", "day", unique=True),)

    app_id: Mapped[UUID] = mapped_column(ForeignKey("channel_apps.id"))
    agent_profile_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    day: Mapped[date] = mapped_column(Date)
    calls: Mapped[int] = mapped_column(Integer, server_default="0")
    tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    estimated_cost: Mapped[float | None] = mapped_column(Float)


class ChannelDelivery(Identity, CreatedAt, UpdatedAt, Base):
    """One message to one destination. The idempotency key prevents duplicates."""

    __tablename__ = "channel_deliveries"
    __table_args__ = (
        Index("uq_channel_deliveries_idempotency_key", "idempotency_key", unique=True),
    )

    destination_id: Mapped[UUID] = mapped_column(ForeignKey("channel_destinations.id"))
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    event_kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    # QUEUED | SENT | FAILED | UNKNOWN (may have reached the platform)
    status: Mapped[str] = mapped_column(String(32), server_default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    external_ref: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(String(64))
    # When an unknown outcome was resolved, and by whom: the platform's own
    # record ("platform") or the owner ("owner").
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_by: Mapped[str | None] = mapped_column(String(32))
    # Which bot sent it; the office keeps every record either way.
    app_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    agent_profile_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Set when the reply belongs in the chat thread the work was requested from.
    thread_ref: Mapped[str | None] = mapped_column(String(64))
    # Slack's buttons for this message, and the files that go with it
    # {"artifact_id", "format"} each, written when it is sent. A file
    # that could not go says why here; the message itself still went.
    blocks_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    files_json: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    files_error: Mapped[str | None] = mapped_column(String(64))
