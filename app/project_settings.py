"""A project's own limits, models and channel.

A project is the office's work area: anything it does not set follows the
office, and anything it does set applies to its own work only. Limits are
checked together, so neither the office's monthly limit nor a project's can be
passed by working inside the other.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ModelConnection, Project, RoomMessage, Topic, TopicItem, UsageEvent
from app.office_settings import (
    MonthlyLimitReached,
    OfficePaused,
    check_monthly_limit,
    default_budget,
    effective,
    month_start,
    paused,
)
from app.office_settings import role_models as office_role_models

FIELDS = (
    "monthly_cost_limit_usd",
    "run_max_tokens",
    "run_max_cost_usd",
    "run_max_minutes",
    "downgrade_at_usd",
    "advisor_on",
    "team_on",
)


def settings_view(project: Project) -> dict[str, Any]:
    return {
        "monthly_cost_limit_usd": project.monthly_cost_limit_usd,
        "run_max_tokens": project.run_max_tokens,
        "run_max_cost_usd": project.run_max_cost_usd,
        "run_max_minutes": project.run_max_minutes,
        "role_models": dict(project.role_models_json or {}),
        "notify_destination_id": str(project.notify_destination_id)
        if project.notify_destination_id
        else None,
        "downgrade_at_usd": project.downgrade_at_usd,
        "advisor_on": project.advisor_on,
        "team_on": project.team_on,
        "cheap_connection_id": str(project.cheap_connection_id)
        if project.cheap_connection_id
        else None,
    }


def save(project: Project, changes: dict[str, Any]) -> None:
    """Store what the owner set. None returns that setting to the office's."""
    for field, value in changes.items():
        if field in FIELDS:
            setattr(project, field, value)
    if "role_models" in changes:
        project.role_models_json = {
            role: str(connection)
            for role, connection in (changes["role_models"] or {}).items()
            if connection
        }
    if "cheap_connection_id" in changes:
        value = changes["cheap_connection_id"]
        project.cheap_connection_id = UUID(str(value)) if value else None
    if "notify_destination_id" in changes:
        value = changes["notify_destination_id"]
        project.notify_destination_id = UUID(str(value)) if value else None


def budget_for(session: Session, settings: Settings, project_id: UUID) -> dict[str, Any]:
    """The budget a new task in this project runs under."""
    office = default_budget(session, settings)
    project = session.get(Project, project_id)
    if project is None:
        return office
    return {
        "max_tokens": project.run_max_tokens or office["max_tokens"],
        "max_cost_usd": project.run_max_cost_usd
        if project.run_max_cost_usd is not None
        else office["max_cost_usd"],
        "max_minutes": project.run_max_minutes
        if project.run_max_minutes is not None
        else office["max_minutes"],
    }


def downgrade_rule(session: Session, project_id: UUID) -> tuple[float, dict[str, Any]] | None:
    """What a task should finish on once it has spent this much, if anything."""
    project = session.get(Project, project_id)
    if project is None or project.downgrade_at_usd is None or project.cheap_connection_id is None:
        return None
    connection = session.get(ModelConnection, project.cheap_connection_id)
    if connection is None:
        return None
    return float(project.downgrade_at_usd), {
        "connection_id": str(connection.id),
        "provider": connection.provider,
        "model_id": connection.model_id,
        "base_url": connection.base_url,
    }


def spent_this_month(session: Session, project_id: UUID, now: datetime | None = None) -> float:
    since = month_start(now)
    total = session.scalar(
        select(func.sum(UsageEvent.estimated_cost)).where(
            UsageEvent.project_id == project_id, UsageEvent.occurred_at >= since
        )
    )
    # What the project's agents said in its room is the project's spending too.
    talk = session.scalar(
        select(func.sum(RoomMessage.cost_usd)).where(
            RoomMessage.project_id == project_id, RoomMessage.created_at >= since
        )
    )
    # Its discussions' briefs and colleagues' checks too.
    briefs = session.scalar(
        select(func.sum(TopicItem.cost_usd))
        .join(Topic, Topic.id == TopicItem.topic_id)
        .where(Topic.project_id == project_id, TopicItem.created_at >= since)
    )
    return float(total or 0) + float(talk or 0) + float(briefs or 0)


def check_limits(
    session: Session, settings: Settings, project_id: UUID, now: datetime | None = None
) -> None:
    """Refuse new work the owner has paused, or past either limit."""
    if paused(session, settings, "new_tasks"):
        raise OfficePaused("new_tasks")
    check_monthly_limit(session, settings, now)
    project = session.get(Project, project_id)
    limit = project.monthly_cost_limit_usd if project else None
    if limit is not None and spent_this_month(session, project_id, now) >= limit:
        raise ProjectLimitReached


class ProjectLimitReached(MonthlyLimitReached):
    """This project has reached its own monthly limit."""


def role_models(session: Session, settings: Settings, project_id: UUID) -> dict[str, Any]:
    """The models this project's roles use: its own choices over the office's."""

    entries = dict(office_role_models(session, settings))
    project = session.get(Project, project_id)
    chosen = dict((project.role_models_json if project else None) or {})
    if not chosen:
        return with_advisor(entries)
    rows = {
        str(row.id): row
        for row in session.scalars(
            select(ModelConnection).where(
                ModelConnection.id.in_([UUID(value) for value in chosen.values()])
            )
        )
    }
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
    return with_advisor(entries)


def pinned_roles(session: Session, project_id: UUID) -> set[str]:
    """Roles this project chose a model for: the model router leaves these alone."""
    project = session.get(Project, project_id)
    return {
        role
        for role, chosen in ((project.role_models_json if project else None) or {}).items()
        if chosen
    }


def with_advisor(entries: dict[str, Any]) -> dict[str, Any]:
    """The advisor reads the course as the Chief does, unless given its own model."""
    if "advisor" not in entries and "chief" in entries:
        entries = entries | {"advisor": entries["chief"]}
    return entries


def advisor_on(session: Session, settings: Settings, project_id: UUID) -> bool:
    """Whether this project's tasks have the advisor on call: its own say, else the office's."""

    project = session.get(Project, project_id)
    if project is not None and project.advisor_on is not None:
        return bool(project.advisor_on)
    return bool(effective(session, settings).get("advisor_on"))


def team_on(session: Session, settings: Settings, project_id: UUID) -> bool:
    """Whether this project's tasks work as a team (T): its own say, else the office's."""
    from app.office_settings import effective

    project = session.get(Project, project_id)
    if project is not None and project.team_on is not None:
        return bool(project.team_on)
    return bool(effective(session, settings).get("team_on"))
