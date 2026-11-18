"""Ideas: thoughts caught in seconds and planned for a day.

An idea is kept the moment the owner writes it, on the floor or with "Note
this" in a discussion, and keeping one never calls a model. The owner plans it
for a day (or someday); on that day it is under Today on the floor, and agents
read it with their work. One planned for a day that has passed stays on top,
marked, and is never dropped quietly. Taking it up as work is a press on
Start, through the discussion hand-off; nothing starts by itself.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget import HANDOFF_COST
from app.calendars.store import preferences, zone_of
from app.db.models import CalendarChange, Idea, Project, Topic

MAX_CHARS = 2_000
# What one list of the board holds; ideas are few, so this is a guard.
LISTED = 50
# What agents are told, at most.
AGENTS_READ = 10
OPEN = ("open", "someday")
FINISHED = ("done", "dropped")
SOURCES = ("floor", "phone", "discussion", "slack")
# The owner's daily three: what was tedious, what caught
# real interest, and a problem someone might pay to have solved. None is unsorted.
KINDS = ("friction", "curiosity", "opportunity")
KIND_NAMES = {"friction": "Friction", "curiosity": "Curiosity", "opportunity": "Opportunity"}


class IdeaRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# The Friday reminder comes from this hour, in the owner's zone.
FRIDAY_HOUR = 16


def owner_zone(session: Session) -> ZoneInfo:
    return zone_of(preferences(session).get("timezone"))


def owner_today(session: Session, now: datetime) -> date:
    """Today, in the owner's own time zone (as their browser last said it)."""
    return now.astimezone(owner_zone(session)).date()


def friday(session: Session, now: datetime) -> bool:
    """Friday to Sunday, in the owner's zone: the board reminds of what waits."""
    return now.astimezone(owner_zone(session)).weekday() >= 4


def bucket(status: str, plan_for: date | None, today: date) -> str:
    """Where an open idea sits: today, overdue, later, someday, or new (unsorted)."""
    if status == "someday":
        return "someday"
    if plan_for is None:
        return "new"
    if plan_for < today:
        return "overdue"
    return "today" if plan_for == today else "later"


def keep(
    session: Session,
    text: str,
    source: str = "floor",
    topic_id: UUID | None = None,
    message_id: int | None = None,
    ref: str | None = None,
    kind: str | None = None,
) -> Idea:
    """Keep a thought as it was written. Free: no model reads it here.

    `ref` names where outside the office it came from (a Slack message); the
    same one is kept once.
    """
    said = (text or "").strip()
    if not said:
        raise IdeaRejected("empty")
    if len(said) > MAX_CHARS:
        raise IdeaRejected("too_long")
    if source not in SOURCES:
        raise IdeaRejected("unknown_source")
    if kind is not None and kind not in KINDS:
        raise IdeaRejected("unknown_kind")
    if ref is not None and session.scalar(select(Idea.id).where(Idea.source_ref == ref)):
        raise IdeaRejected("already_kept")
    idea = Idea(
        text=said,
        source=source,
        topic_id=topic_id,
        source_message_id=message_id,
        source_ref=ref,
        kind=kind,
    )
    session.add(idea)
    session.flush()
    return idea


def plan(session: Session, idea: Idea, day: date | None, someday: bool, now: datetime) -> None:
    """Plan it for a day, or for someday; neither one starts anything."""
    if idea.status not in OPEN:
        raise IdeaRejected("already_done")
    if someday:
        idea.status, idea.plan_for = "someday", None
        return
    if day is not None and day < owner_today(session, now):
        raise IdeaRejected("in_the_past")
    idea.status, idea.plan_for = "open", day


def set_kind(idea: Idea, kind: str | None) -> None:
    """Friction, curiosity or opportunity; None puts it back with the unsorted."""
    if kind is not None and kind not in KINDS:
        raise IdeaRejected("unknown_kind")
    if idea.status not in OPEN:
        raise IdeaRejected("already_done")
    idea.kind = kind


def finish(idea: Idea, status: str) -> None:
    """Done (handled by the owner) or dropped. Either way it leaves the board."""
    if status not in FINISHED:
        raise IdeaRejected("unknown_status")
    if idea.status not in OPEN:
        raise IdeaRejected("already_done")
    idea.status = status


def set_project(idea: Idea, project: Project | None) -> None:
    """Which project it belongs to; its discussion starts there (or nowhere)."""
    if idea.status not in OPEN:
        raise IdeaRejected("already_done")
    idea.project_id = project.id if project is not None else None


def block(session: Session, idea: Idea, change: CalendarChange | None) -> None:
    """Remember the time the owner blocked for it: the new calendar event they
    saved from it. Its start's day, in the owner's zone, becomes its day."""
    if idea.status not in OPEN:
        raise IdeaRejected("already_done")
    if change is None or change.action != "create":
        raise IdeaRejected("not_a_new_event")
    try:
        starts = datetime.fromisoformat(str((change.payload_json or {}).get("starts_at")))
    except ValueError:
        raise IdeaRejected("not_a_new_event") from None
    if starts.tzinfo is None:
        raise IdeaRejected("not_a_new_event")
    zone = zone_of(preferences(session).get("timezone"))
    idea.blocked_at, idea.calendar_change_id = starts, change.id
    idea.status, idea.plan_for = "open", starts.astimezone(zone).date()


def view(
    idea: Idea, today: date, topic: Topic | None = None, project: Project | None = None
) -> dict[str, Any]:
    return {
        "id": str(idea.id),
        "text": idea.text,
        "kind": idea.kind,
        "at": idea.created_at.isoformat() if idea.created_at else None,
        "source": idea.source,
        "status": idea.status,
        "plan_for": idea.plan_for.isoformat() if idea.plan_for else None,
        "bucket": bucket(idea.status, idea.plan_for, today),
        "topic": {"id": str(topic.id), "title": topic.title} if topic is not None else None,
        "run_id": str(idea.run_id) if idea.run_id else None,
        "project": {"id": str(project.id), "name": project.name} if project is not None else None,
        "blocked_at": idea.blocked_at.isoformat() if idea.blocked_at else None,
    }


def _open(session: Session) -> list[tuple[Idea, Topic | None, Project | None]]:
    return [
        (idea, topic, project)
        for idea, topic, project in session.execute(
            select(Idea, Topic, Project)
            .outerjoin(Topic, Topic.id == Idea.topic_id)
            .outerjoin(Project, Project.id == Idea.project_id)
            .where(Idea.status.in_(OPEN))
            .order_by(Idea.created_at.desc(), Idea.id.desc())
        )
    ]


def board(session: Session, now: datetime) -> dict[str, Any]:
    """The floor's Ideas: Today (overdue first, oldest day first), new and
    unsorted (newest first), later (soonest first), and someday."""
    today = owner_today(session, now)
    lists: dict[str, list[dict[str, Any]]] = {
        "today": [],
        "new": [],
        "later": [],
        "someday": [],
    }
    for idea, topic, project in _open(session):
        shown = view(idea, today, topic, project)
        lists["today" if shown["bucket"] == "overdue" else shown["bucket"]].append(shown)
    lists["today"].sort(key=lambda i: (i["plan_for"], i["at"] or ""))
    lists["later"].sort(key=lambda i: (i["plan_for"], i["at"] or ""))
    # The same open ideas by kind, newest first (the wall's default view).
    kinds: dict[str, list[dict[str, Any]]] = {kind: [] for kind in (*KINDS, "unsorted")}
    for items in lists.values():
        for shown in items:
            kinds[shown["kind"] or "unsorted"].append(shown)
    for items in kinds.values():
        items.sort(key=lambda i: i["at"] or "", reverse=True)
    return {
        "today_is": today.isoformat(),
        # Friday to Sunday the board asks for what is not planned yet.
        "weekend": friday(session, now),
        # What Start may spend before it asks (the discussion hand-off's cap).
        "start_cap_usd": HANDOFF_COST,
        **{name: items[:LISTED] for name, items in lists.items()},
        "counts": {name: len(items) for name, items in lists.items()},
        "kinds": {name: items[:LISTED] for name, items in kinds.items()},
        "kind_counts": {name: len(items) for name, items in kinds.items()},
        "today_three": todays_three(session, now),
    }


def todays_three(session: Session, now: datetime) -> dict[str, int]:
    """How many of each kind the owner kept today, in their own zone, whatever
    became of them since: the daily practice is the keeping."""
    zone = owner_zone(session)
    local = now.astimezone(zone)
    start = datetime(local.year, local.month, local.day, tzinfo=zone)
    counted = dict.fromkeys(KINDS, 0)
    for (kind,) in session.execute(
        select(Idea.kind).where(Idea.kind.in_(KINDS), Idea.created_at >= start)
    ):
        counted[kind] += 1
    return counted


def of_topic(session: Session, topic_id: UUID) -> list[dict[str, Any]]:
    """The open ideas noted in one discussion, oldest first (its side panel)."""
    return [
        {"id": str(idea.id), "text": idea.text, "at": idea.created_at.isoformat()}
        for idea in session.scalars(
            select(Idea)
            .where(Idea.topic_id == topic_id, Idea.status.in_(OPEN))
            .order_by(Idea.created_at)
        )
    ]


def agent_context(session: Session, now: datetime, purpose: str) -> str:
    """What agents read: only the ideas the owner planned for today, or
    for a day that has passed. Unsorted and later ones stay the owner's."""
    today = owner_today(session, now)
    due = session.scalars(
        select(Idea)
        .where(Idea.status == "open", Idea.plan_for.is_not(None), Idea.plan_for <= today)
        .order_by(Idea.plan_for, Idea.created_at)
        .limit(AGENTS_READ)
    ).all()
    if not due:
        return ""
    zone = zone_of(preferences(session).get("timezone"))
    lines = [
        f"- {KIND_NAMES[idea.kind] + ': ' if idea.kind in KIND_NAMES else ''}{idea.text[:200]}"
        + ("" if idea.plan_for == today else f" (planned for {idea.plan_for:%a %d %b})")
        + (
            f" (time blocked at {idea.blocked_at.astimezone(zone):%H:%M})"
            if idea.blocked_at
            else ""
        )
        for idea in due
        if idea.plan_for is not None
    ]
    return "Ideas the owner planned for today (theirs to take up; nothing is started):\n" + (
        "\n".join(lines)
    )
