"""Tables for the office itself: models, agents, policies, settings, skills."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, CreatedAt, Identity, UpdatedAt


class ModelConnection(Identity, CreatedAt, UpdatedAt, Base):
    """Owner-scoped provider metadata; the credential itself never lives here."""

    __tablename__ = "model_connections"
    __table_args__ = (
        Index("uq_model_connections_provider_name", "provider", "display_name", unique=True),
    )

    provider: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    model_id: Mapped[str] = mapped_column(String(255))
    # The provider's API address. Null uses the SDK's own default.
    base_url: Mapped[str | None] = mapped_column(String(1000))
    credential_ref: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), server_default="CONFIGURED", index=True)
    capabilities_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class AgentProfile(Identity, CreatedAt, UpdatedAt, Base):
    """A reusable agent. Its configuration lives in immutable versions."""

    __tablename__ = "agent_profiles"
    __table_args__ = (Index("uq_agent_profiles_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), server_default="ACTIVE", index=True)
    # The version new tasks use. Enabling a version is an explicit owner action.
    enabled_version: Mapped[int | None] = mapped_column(Integer)


class AgentProfileVersion(Identity, CreatedAt, Base):
    """One immutable agent configuration. Editing creates a new version."""

    __tablename__ = "agent_profile_versions"
    __table_args__ = (
        Index("uq_agent_profile_versions_profile_version", "profile_id", "version", unique=True),
    )

    profile_id: Mapped[UUID] = mapped_column(ForeignKey("agent_profiles.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    instructions: Mapped[str] = mapped_column(Text, server_default="")
    # Assigning a connection is the grant that lets this agent use it.
    connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_connections.id"), index=True
    )
    limits_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Pinned skills: [{"skill_id", "version"}]. Immutable like the version.
    skills_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class OwnerSession(Identity, CreatedAt, Base):
    """A browser session for the local owner. Only a hash of the cookie is kept."""

    __tablename__ = "owner_sessions"
    __table_args__ = (Index("uq_owner_sessions_token_hash", "token_hash", unique=True),)

    token_hash: Mapped[str] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "Keep me signed in on this device": a month, not twelve idle hours.
    remember: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Policy(Identity, CreatedAt, UpdatedAt, Base):
    """One rule the owner turned on, and where it applies."""

    __tablename__ = "policies"
    __table_args__ = (Index("ix_policies_scope_project", "scope", "project_id"),)

    kind: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16), server_default="office")
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    params_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class PolicyRecord(CreatedAt, Base):
    """What the office decided, and why. Kept whether it allowed or refused."""

    __tablename__ = "policy_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    project_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    hook: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(128))
    verdict: Mapped[str] = mapped_column(String(16))
    policy: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(String(16))


class PolicyState(Base):
    """What one run has done so far, for policies that count."""

    __tablename__ = "policy_state"

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    counters_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OfficeSettings(Identity, CreatedAt, UpdatedAt, Base):
    """The office's own settings, changed by the owner in the browser.

    One row. A null value means "use the value from configuration", so an
    owner can clear a setting and return to the configured default.
    """

    __tablename__ = "office_settings"
    __table_args__ = (Index("uq_office_settings_single_row", "only", unique=True),)

    only: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    run_max_tokens: Mapped[int | None] = mapped_column(Integer)
    run_max_cost_usd: Mapped[float | None] = mapped_column(Float)
    run_max_minutes: Mapped[int | None] = mapped_column(Integer)
    # New work is refused once this month's recorded cost passes this.
    monthly_cost_limit_usd: Mapped[float | None] = mapped_column(Float)
    chat_daily_max_tokens: Mapped[int | None] = mapped_column(Integer)
    # What the owner calls this office and themselves, set in the browser.
    office_name: Mapped[str | None] = mapped_column(String(128))
    owner_name: Mapped[str | None] = mapped_column(String(255))
    setup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Switches the owner can reach. Each stops new work of one kind; work
    # already running finishes.
    pause_new_tasks: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pause_schedules: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pause_chat: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The advisor on call: off until the owner turns it on.
    advisor_on: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    # Team mode (T): agents read the room while they work, and huddle.
    team_on: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    # The office's own browser: off until the owner turns it on.
    browser_on: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    # How AI history is protected (app/archive/protection.py). Empty means the defaults.
    archive_protection_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Which coding agent builds: codex or claude_code; null is Codex.
    coding_agent: Mapped[str | None] = mapped_column(String(20))
    # Whether builds may install packages from npm and PyPI. Off until set.
    build_packages: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    # The owner's sign-in name and password, as a salted scrypt hash.
    owner_login_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Related work from another business line, only when the owner allows.
    related_across_lines: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    # Tasks run at the same time; empty follows the configuration (MW, 0043).
    runs_at_once: Mapped[int | None] = mapped_column(Integer)
    # How a task's model is chosen: "auto" routes each task to
    # the cheapest tier likely to do it well; "fixed" keeps the role models. Null is auto.
    model_routing: Mapped[str | None] = mapped_column(String(16))
    # role -> model connection id, used when no agent for that role names one.
    role_models_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # The backup the owner chose per provider (a connection id or "gateway"):
    # {"backup": <key>, "come_back": bool}. None chosen, a failure stays one.
    provider_backups_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # How the owner's calendars shape the work: what agents may see
    # ("busy", "titles", "all"), holding the in-tray in meetings, prep offers,
    # and the prep offers the owner turned down.
    calendar_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class InstalledSkill(Identity, CreatedAt, UpdatedAt, Base):
    """One installed version of a curated skill. Uninstalled rows are kept."""

    __tablename__ = "installed_skills"
    __table_args__ = (
        Index("uq_installed_skills_skill_version", "skill_id", "version", unique=True),
    )

    skill_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    # ENABLED | DISABLED | NEEDS_REVIEW | UNINSTALLED
    status: Mapped[str] = mapped_column(String(32))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillUpdate(Identity, Base):
    """Whether a skill from GitHub has changed since the commit installed."""

    __tablename__ = "skill_updates"

    skill_id: Mapped[str] = mapped_column(String(64), unique=True)
    repo: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(512))
    installed_commit: Mapped[str] = mapped_column(String(40))
    latest_commit: Mapped[str | None] = mapped_column(String(40))
    latest_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    # current | available | unreachable
    state: Mapped[str] = mapped_column(String(16), server_default="current")
    reason: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfficeWidget(Identity, CreatedAt, UpdatedAt, Base):
    """A widget on the owner's wall on the floor.

    One row per kind, made the first time the wall is read. Taking a widget
    off keeps the row (placed false), so putting it back restores its place;
    what it knew is kept or cleaned by the widget itself, not here.
    """

    __tablename__ = "office_widgets"

    kind: Mapped[str] = mapped_column(String(40), unique=True)
    placed: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # "wide" across the floor, "small" in the side column, "folded" to a line.
    size: Mapped[str] = mapped_column(String(12), server_default=text("'small'"))
    # Whether agents read it with their work.
    agents_read: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccessPoint(Base):
    """An outside address the office can be opened at: the home network
    or the private remote route, what was verified about it, and whether a
    phone has been confirmed through it."""

    __tablename__ = "access_points"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    address: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_device: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
