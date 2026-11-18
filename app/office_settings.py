"""Office settings the owner controls, and the monthly spending limit.

Configuration in `.env` is the floor: a setting the owner has not changed
follows it. A stored value overrides it for new work only, so a run already
executing keeps the budget it started with.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    ArchiveReading,
    ChannelUsage,
    IdeaSort,
    ModelConnection,
    OfficeSettings,
    RoomMessage,
    TopicItem,
    UsageEvent,
    WatchCheck,
)

FIELDS = (
    "run_max_tokens",
    "run_max_cost_usd",
    "run_max_minutes",
    "monthly_cost_limit_usd",
    "chat_daily_max_tokens",
    "office_name",
    "owner_name",
    "pause_new_tasks",
    "pause_schedules",
    "pause_chat",
    "advisor_on",
    "team_on",
    "browser_on",
    "runs_at_once",
    "related_across_lines",
    "model_routing",
)


class MonthlyLimitReached(Exception):
    """New work would run past the owner's monthly spending limit."""


class OfficePaused(Exception):
    """The owner has paused new work of this kind."""

    def __init__(self, what: str) -> None:
        self.what = what
        super().__init__(what)


def paused(session: Session, settings: Settings, what: str) -> bool:
    """Whether the owner has switched off new work of this kind."""
    return bool(effective(session, settings).get(f"pause_{what}", False))


def _row(session: Session) -> OfficeSettings | None:
    return session.scalar(select(OfficeSettings).limit(1))


def chat_allowance(session: Session, settings: Settings) -> int:
    """Tokens each bot may use in a day: what the owner set, else the configured default."""
    return int(effective(session, settings)["chat_daily_max_tokens"])


def effective(session: Session, settings: Settings) -> dict[str, Any]:
    """The settings in force: stored values over configured defaults."""
    row = _row(session)
    stored = {field: getattr(row, field, None) for field in FIELDS}
    return {
        "run_max_tokens": stored["run_max_tokens"] or settings.run_max_tokens,
        "run_max_cost_usd": stored["run_max_cost_usd"]
        if stored["run_max_cost_usd"] is not None
        else settings.run_max_cost_usd,
        "run_max_minutes": stored["run_max_minutes"]
        if stored["run_max_minutes"] is not None
        else settings.run_max_minutes,
        "monthly_cost_limit_usd": stored["monthly_cost_limit_usd"],
        "chat_daily_max_tokens": stored["chat_daily_max_tokens"] or settings.chat_daily_max_tokens,
        "role_models": dict(getattr(row, "role_models_json", None) or {}),
        # The owner's own names win over configuration, so setup needs no .env.
        "office_name": stored["office_name"] or settings.office_name,
        "owner_name": stored["owner_name"] or settings.owner_name,
        "setup_completed_at": getattr(row, "setup_completed_at", None),
        "pause_new_tasks": bool(getattr(row, "pause_new_tasks", False)),
        "pause_schedules": bool(getattr(row, "pause_schedules", False)),
        "pause_chat": bool(getattr(row, "pause_chat", False)),
        "advisor_on": bool(getattr(row, "advisor_on", False)),
        "team_on": bool(getattr(row, "team_on", False)),
        "browser_on": bool(getattr(row, "browser_on", False)),
        "runs_at_once": stored["runs_at_once"] or settings.max_concurrent_runs,
        "related_across_lines": bool(stored["related_across_lines"]),
        "model_routing": stored["model_routing"] or "auto",
    }


def save(session: Session, changes: dict[str, Any]) -> None:
    """Store the owner's settings. None clears one back to configuration."""
    row = _row(session)
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    for field, value in changes.items():
        if field in FIELDS:
            setattr(row, field, value)
    if "role_models" in changes:
        # A role mapped to nothing falls back to the office's model gateway.
        chosen = {
            role: str(connection)
            for role, connection in (changes["role_models"] or {}).items()
            if connection
        }
        row.role_models_json = chosen
    session.flush()


def role_models(session: Session, settings: Settings) -> dict[str, dict[str, Any]]:
    """The connection each role uses by default, as snapshot-shaped entries."""
    chosen = effective(session, settings)["role_models"]
    if not chosen:
        return {}
    rows = {
        str(row.id): row
        for row in session.scalars(
            select(ModelConnection).where(
                ModelConnection.id.in_([UUID(value) for value in chosen.values()])
            )
        )
    }
    entries = {}
    for role, connection_id in chosen.items():
        connection = rows.get(str(connection_id))
        if connection is None:
            continue
        entries[role] = {
            "connection_id": str(connection.id),
            "provider": connection.provider,
            "model_id": connection.model_id,
            "base_url": connection.base_url,
        }
    return entries


def default_budget(session: Session, settings: Settings) -> dict[str, Any]:
    current = effective(session, settings)
    return {
        "max_tokens": current["run_max_tokens"],
        "max_cost_usd": current["run_max_cost_usd"],
        "max_minutes": current["run_max_minutes"],
    }


def month_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(session: Session, now: datetime | None = None) -> float:
    """Recorded cost this month, from runs, chat and project rooms. Unpriced calls
    count as zero."""
    since = month_start(now)
    runs = session.scalar(
        select(func.sum(UsageEvent.estimated_cost)).where(UsageEvent.occurred_at >= since)
    )
    chat = session.scalar(
        select(func.sum(ChannelUsage.estimated_cost)).where(ChannelUsage.day >= since.date())
    )
    rooms = session.scalar(
        select(func.sum(RoomMessage.cost_usd)).where(RoomMessage.created_at >= since)
    )
    # A discussion's brief and colleagues' checks.
    topics = session.scalar(
        select(func.sum(TopicItem.cost_usd)).where(TopicItem.created_at >= since)
    )
    # Watching pages.
    watched = session.scalar(select(func.sum(WatchCheck.cost_usd)).where(WatchCheck.at >= since))
    # Sort my ideas.
    sorts = session.scalar(select(func.sum(IdeaSort.cost_usd)).where(IdeaSort.created_at >= since))
    # Reading a conversation for what to keep.
    readings = session.scalar(
        select(func.sum(ArchiveReading.cost_usd)).where(ArchiveReading.created_at >= since)
    )
    return (
        float(runs or 0)
        + float(readings or 0)
        + float(chat or 0)
        + float(rooms or 0)
        + float(topics or 0)
        + float(sorts or 0)
        + float(watched or 0)
    )


def check_monthly_limit(session: Session, settings: Settings, now: datetime | None = None) -> None:
    """Refuse to start new work once the owner's monthly limit is reached."""
    limit = effective(session, settings)["monthly_cost_limit_usd"]
    if limit is not None and spent_this_month(session, now) >= limit:
        raise MonthlyLimitReached


def provider_backups(session: Session) -> dict[str, dict[str, Any]]:
    """The backup chosen for each provider, keyed by connection id or "gateway"."""
    row = _row(session)
    stored = dict(getattr(row, "provider_backups_json", None) or {})
    return {key: dict(value) for key, value in stored.items() if isinstance(value, dict)}


def set_backup(session: Session, key: str, backup: str | None, come_back: bool = True) -> None:
    """Choose a provider's backup, or none. The caller checks both exist."""
    row = _row(session)
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    chosen = dict(row.provider_backups_json or {})
    if backup is None:
        chosen.pop(key, None)
    else:
        chosen[key] = {"backup": backup, "come_back": bool(come_back)}
    # A new dict, so the JSON column is seen to have changed.
    row.provider_backups_json = chosen
    session.flush()
