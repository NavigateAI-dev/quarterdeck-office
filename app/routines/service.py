"""Keeping routines: what one is, and what the owner and agents may do with it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import topics
from app.calendars.store import preferences, zone_of
from app.channels import slack_format
from app.db.models import (
    ChannelDelivery,
    ChannelDestination,
    ChannelInstallation,
    Project,
    RoomMessage,
    Schedule,
    Topic,
)
from app.scheduling import Recurrence, next_occurrence

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# What a reply starts with when a routine has nothing worth telling.
QUIET = "NOTHING TO REPORT"
ASKED = (
    "Do this as you would if I asked you here myself: follow what I have told you I want, and "
    "write in the language I use with you. Search the web for anything current and read the "
    "pages before relying on them. Use my calendar and connected tools. Name your sources. If a "
    "source could not be read or has nothing, say so in one line. This runs on its own, so finish "
    "it now: do not ask me anything or propose a task."
)
# A routine this late is about a moment that has passed: the computer was asleep.
LATE_LIMIT = timedelta(hours=4)
MISSED_EVENT = "routine_missed"
EVENT = "routine"
MISSED = {
    "skipped_too_late": "the office was not running at the set time, usually because the "
    "computer was asleep",
    "skipped_paused": "scheduled tasks are paused in Settings",
}


class RoutineRejected(ValueError):
    """A routine that cannot be set as asked; the message says why."""


def rule_from(values: dict[str, Any], timezone: str | None) -> Recurrence:
    """A timing from what was asked. Raises RoutineRejected with the reason."""
    try:
        return Recurrence(
            timezone=timezone or "UTC",
            frequency=str(values.get("frequency") or "daily"),
            local_time=str(values.get("local_time") or "09:00"),
            weekday=values.get("weekday"),
            every_hours=values.get("every_hours"),
            cron=values.get("cron"),
            once_at=values.get("once_at"),
        )
    except ValidationError as wrong:
        reason = wrong.errors()[0]["msg"].removeprefix("Value error, ")
        raise RoutineRejected(reason) from None


def owner_timezone(session: Session) -> str:
    return str(zone_of(preferences(session).get("timezone")))


def rule_of(row: Schedule) -> Recurrence:
    return Recurrence(
        timezone=row.timezone,
        frequency=row.frequency,
        local_time=row.local_time,
        weekday=row.weekday,
        every_hours=row.every_hours,
        cron=row.cron,
        once_at=row.once_at,
    )


def when(rule: Recurrence) -> str:
    """The timing in words."""
    if rule.frequency == "once":
        return f"once, at {rule.once_at:%Y-%m-%d %H:%M}" if rule.once_at else "once"
    if rule.frequency == "hours":
        return f"every {rule.every_hours} hours"
    if rule.frequency == "cron":
        return f"on the schedule {rule.cron}"
    if rule.frequency == "weekly" and rule.weekday is not None:
        return f"every {DAYS[rule.weekday]} at {rule.local_time}"
    return f"every day at {rule.local_time}"


def asked_of(row: Schedule) -> str:
    """The message the agent answers when this routine comes due."""
    quiet = (
        f"\n\nIf {row.quiet_when.strip()}, reply with exactly {QUIET} and nothing else."
        if row.quiet_when.strip()
        else ""
    )
    return f"{row.title} (a routine, not typed by me):\n{row.prompt}\n\n{ASKED}{quiet}"


def _found(session: Session, routine_id: UUID) -> Schedule | None:
    row = session.get(Schedule, routine_id)
    return row if row is not None and row.prompt is not None else None


def found(session: Session, routine_id: UUID) -> Schedule:
    row = _found(session, routine_id)
    if row is None:
        raise RoutineRejected("no such routine")
    return row


def view(session: Session, row: Schedule) -> dict[str, Any]:
    """One routine, as the page shows it."""
    last = session.get(RoomMessage, row.last_message_id) if row.last_message_id else None
    reply = (
        session.scalar(
            select(RoomMessage)
            .where(RoomMessage.reply_to == last.id)
            .order_by(RoomMessage.id.desc())
            .limit(1)
        )
        if last is not None
        else None
    )
    topic = session.get(Topic, row.topic_id) if row.topic_id else None
    return {
        "id": str(row.id),
        "title": row.title,
        "prompt": row.prompt,
        "quiet_when": row.quiet_when,
        "enabled": row.enabled,
        "disabled_reason": row.disabled_reason,
        "frequency": row.frequency,
        "local_time": row.local_time,
        "weekday": row.weekday,
        "every_hours": row.every_hours,
        "cron": row.cron,
        "once_at": row.once_at.isoformat() if row.once_at else None,
        "timezone": row.timezone,
        "when": when(rule_of(row)),
        "next_run_at": row.next_run_at.isoformat() if row.enabled and row.next_run_at else None,
        "topic_id": str(row.topic_id) if row.topic_id else None,
        "topic_title": topic.title if topic else None,
        "post_to": str(row.post_to_id) if row.post_to_id else None,
        "preset": row.preset,
        "last_outcome": row.last_outcome,
        "last_cost_usd": reply.cost_usd if reply is not None else None,
        "last_at": last.created_at.isoformat() if last is not None else None,
    }


def listed(session: Session) -> list[Schedule]:
    return list(
        session.scalars(
            select(Schedule).where(Schedule.prompt.is_not(None)).order_by(Schedule.created_at)
        )
    )


def told(session: Session) -> str:
    """What an agent is told about the routines already running."""
    rows = [row for row in listed(session) if row.enabled]
    set_up = "\n".join(f"- {row.id}: {row.title}, {when(rule_of(row))}" for row in rows)
    return (
        "The office can repeat work on its own: propose it with routine_create, and propose a "
        "change with routine_update when the owner changes what they want from one. It takes the "
        "owner's press, and no engineering.\n" + (set_up or "No routine is set yet.")
    )


def create(
    session: Session,
    *,
    title: str,
    prompt: str,
    rule: Recurrence,
    now: datetime,
    topic_id: UUID | None = None,
    project: Project | None = None,
    post_to: UUID | None = None,
    quiet_when: str = "",
    preset: str | None = None,
    agent_profile_id: UUID | None = None,
    enabled: bool = True,
) -> Schedule:
    """Set a routine. With no discussion given, it starts one of its own."""
    upcoming = next_occurrence(rule, now)
    if upcoming is None:
        raise RoutineRejected("that time has already passed")
    if topic_id is None:
        topic = topics.start(session, title, project)
        topic.title = title[:200]
        session.flush()
        topic_id = topic.id
    row = Schedule(
        title=title[:200],
        prompt=prompt.strip(),
        quiet_when=quiet_when.strip(),
        timezone=rule.timezone,
        frequency=rule.frequency,
        local_time=rule.local_time,
        weekday=rule.weekday,
        every_hours=rule.every_hours,
        cron=rule.cron,
        once_at=rule.once_at,
        enabled=enabled,
        disabled_reason=None if enabled else "disabled_by_owner",
        next_run_at=upcoming,
        topic_id=topic_id,
        post_to_id=post_to,
        preset=preset,
        agent_profile_id=agent_profile_id,
    )
    session.add(row)
    session.flush()
    return row


def update(session: Session, row: Schedule, changes: dict[str, Any], now: datetime) -> Schedule:
    """Change what was given. A new timing starts from the next time it falls due."""
    for name in ("title", "prompt", "quiet_when"):
        if changes.get(name) is not None:
            setattr(row, name, str(changes[name]).strip())
    if "post_to" in changes:
        row.post_to_id = changes["post_to"]
    if "topic_id" in changes and changes["topic_id"] is not None:
        row.topic_id = changes["topic_id"]
    timing = ("frequency", "local_time", "weekday", "every_hours", "cron", "once_at", "timezone")
    if any(changes.get(name) is not None for name in timing):
        current = rule_of(row).model_dump()
        current.update({k: changes[k] for k in timing if changes.get(k) is not None})
        rule = rule_from(current, str(current["timezone"]))
        row.timezone = rule.timezone
        row.frequency = rule.frequency
        row.local_time = rule.local_time
        row.weekday = rule.weekday
        row.every_hours = rule.every_hours
        row.cron = rule.cron
        row.once_at = rule.once_at
    if changes.get("enabled") is not None:
        row.enabled = bool(changes["enabled"])
        row.disabled_reason = None if row.enabled else "disabled_by_owner"
    if row.enabled:
        row.next_run_at = next_occurrence(rule_of(row), now)
        if row.next_run_at is None:
            raise RoutineRejected("that time has already passed")
    session.flush()
    return row


def stop(session: Session, row: Schedule) -> None:
    row.enabled = False
    row.disabled_reason = "disabled_by_owner"
    session.flush()


def history(session: Session, row: Schedule, limit: int = 10) -> list[dict[str, Any]]:
    """The latest times it came due, each with the answer it got."""
    asked = session.scalars(
        select(RoomMessage)
        .where(RoomMessage.client_key.like(f"routine:{row.id}:%"))
        .order_by(RoomMessage.id.desc())
        .limit(limit)
    )
    seen = []
    for message in asked:
        reply = session.scalar(
            select(RoomMessage)
            .where(RoomMessage.reply_to == message.id)
            .order_by(RoomMessage.id.desc())
            .limit(1)
        )
        seen.append(
            {
                "at": message.created_at.isoformat(),
                "topic_id": str(message.topic_id),
                "answer": reply.body if reply is not None else None,
                "by": reply.author_name if reply is not None else None,
                "cost_usd": reply.cost_usd if reply is not None else None,
            }
        )
    return seen


def post_to_channel(
    session: Session, row: Schedule, text: str, kind: str, key: str, formatted: bool = False
) -> None:
    """Queue a message for the channel the owner chose, once by its key."""
    destination = session.get(ChannelDestination, row.post_to_id) if row.post_to_id else None
    installation = (
        session.get(ChannelInstallation, destination.installation_id) if destination else None
    )
    if destination is None or installation is None or installation.status != "ACTIVE":
        return
    session.execute(
        insert(ChannelDelivery)
        .values(
            destination_id=destination.id,
            event_kind=kind,
            idempotency_key=key,
            text=text,
            status="QUEUED",
            app_id=installation.app_id,
            blocks_json=slack_format.sections(text) if formatted else None,
        )
        .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
    )


def say_missed(session: Session, row: Schedule, outcome: str) -> None:
    """Tell the owner, in the discussion and the channel, that this one is not coming."""
    why = MISSED.get(outcome)
    if why is None:
        return
    text = f"No {row.title} this time: {why}."
    if row.topic_id is not None:
        session.add(
            RoomMessage(
                topic_id=row.topic_id,
                author_kind="office",
                author_name="The office",
                body=text,
            )
        )
        topics.touch(session, row.topic_id)
    due = row.next_run_at or datetime.now(UTC)
    post_to_channel(session, row, text, MISSED_EVENT, f"routine-missed:{row.id}/{due.isoformat()}")
