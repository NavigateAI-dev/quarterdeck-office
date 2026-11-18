"""The owner's wall: widgets added to the floor and taken off again.

A widget is four things: a tile on the floor (drawn by the browser), a short
context agents may read with their work, what it holds of its own, and a way
to clean that out. The wall records only where each widget sits and whether
agents read it. Taking a widget off keeps what it knew unless the owner asks
for it to be cleaned; putting it back restores it.

Widgets are part of the office, never installed from outside.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import ideas, watches
from app.calendars.store import agent_view, preferences
from app.db.models import (
    CalendarEvent,
    CalendarSource,
    Idea,
    OfficeSettings,
    OfficeWidget,
    Run,
    Task,
    Topic,
)
from app.office_overview import _month_start, _usage
from app.titles import title_of

logger = logging.getLogger(__name__)

SIZES = ("wide", "small", "folded")
# All the widgets agents read, together, stay within this.
CONTEXT_BUDGET = 6_000
DISCUSSION_DAYS = 7
TASK_DAYS = 2


class WidgetRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class Services:
    """What cleaning a widget out may need beyond the database."""

    session_factory: sessionmaker[Session]
    calendars: Any = None


@dataclass(frozen=True)
class Widget:
    kind: str
    title: str
    blurb: str
    size: str
    # On the wall in a new office, and read by agents unless switched off.
    placed: bool
    agents_read: bool
    # What agents are told, for a task ("task") or a discussion ("discussion").
    context: Callable[[Session, datetime, str], str] | None = None
    # What it keeps of its own, in words the owner reads before cleaning.
    held: Callable[[Session], list[str]] = field(default=lambda session: [])
    forget: Callable[[Services], None] | None = None


# ---- the calendar ----


def _calendar_context(session: Session, now: datetime, purpose: str) -> str:
    days = DISCUSSION_DAYS if purpose == "discussion" else TASK_DAYS
    return agent_view(session, now, days=days)


def _calendar_held(session: Session) -> list[str]:
    sources = int(session.scalar(select(func.count(CalendarSource.id))) or 0)
    events = int(session.scalar(select(func.count(CalendarEvent.id))) or 0)
    if not sources:
        return []
    return [
        f"{sources} connected calendar{'' if sources == 1 else 's'} and their sign-ins",
        f"{events} synced event{'' if events == 1 else 's'}",
        "your calendar choices (what agents may see, holding the in-tray, prep offers)",
    ]


def _calendar_forget(services: Services) -> None:
    with services.session_factory() as session:
        ids = list(session.scalars(select(CalendarSource.id)))
    for source_id in ids:
        if services.calendars is not None:
            # Deletes its events and its sealed sign-in as well.
            services.calendars.disconnect(source_id)
        else:
            with services.session_factory() as session, session.begin():
                row = session.get(CalendarSource, source_id)
                if row is not None:
                    session.delete(row)
    with services.session_factory() as session, session.begin():
        settings = session.scalar(select(OfficeSettings).limit(1))
        if settings is not None:
            zone = preferences(session).get("timezone")
            # The time zone is the browser's, not something the widget learned.
            settings.calendar_json = {"timezone": zone} if zone else {}


# ---- this month's spending ----


def _month_context(session: Session, now: datetime, purpose: str) -> str:

    usage = _usage(session, _month_start(now))
    partly = "" if usage.get("cost_complete") else " (some of it unpriced)"
    return (
        f"The office's spending this month: ${float(usage.get('cost_usd') or 0):.2f}{partly} "
        f"over {usage.get('calls', 0)} model calls."
    )


# ---- delivered this week ----


def _delivered_context(session: Session, now: datetime, purpose: str) -> str:
    rows = session.execute(
        select(Task.title, Task.objective, Run.completed_at)
        .join(Task, Task.id == Run.root_task_id)
        .where(
            Run.status.in_(("COMPLETED", "CLOSED")),
            Run.completed_at >= now - timedelta(days=7),
        )
        .order_by(Run.completed_at.desc())
        .limit(10)
    ).all()
    if not rows:
        return "Delivered this week: nothing yet."
    lines = [
        f"- {title_of(title, objective)} ({completed:%a %d %b})"
        for title, objective, completed in rows
        if completed is not None
    ]
    return "Work the office delivered this week:\n" + "\n".join(lines)


# ---- ideas ----


def _ideas_held(session: Session) -> list[str]:
    count = int(session.scalar(select(func.count(Idea.id)).where(Idea.status.in_(ideas.OPEN))) or 0)
    # The ideas are the owner's, not the widget's: taking it off keeps them.
    return [f"{count} open idea{'' if count == 1 else 's'}"] if count else []


# ---- parked for later ----


def _parked_context(session: Session, now: datetime, purpose: str) -> str:
    returning = session.execute(
        select(Topic.title, Topic.revisit_at)
        .where(Topic.revisit_at.is_not(None), Topic.revisit_at > now)
        .order_by(Topic.revisit_at)
        .limit(10)
    ).all()
    if not returning:
        return ""
    lines = [f"- Coming back {when:%a %d %b}: {title}" for title, when in returning if when]
    return "Discussions the owner parked for later:\n" + "\n".join(lines)


CATALOG: dict[str, Widget] = {
    widget.kind: widget
    for widget in (
        Widget(
            "calendar",
            "Your day",
            "Your calendar's day on the floor. Agents plan around your schedule.",
            "wide",
            placed=True,
            agents_read=True,
            context=_calendar_context,
            held=_calendar_held,
            forget=_calendar_forget,
        ),
        Widget(
            "ideas",
            "Ideas",
            "Catch a thought in seconds and plan it for a day. Agents read the ones for today.",
            # A board under your day: Today, New and Coming up.
            "wide",
            placed=True,
            agents_read=True,
            context=ideas.agent_context,
            held=_ideas_held,
        ),
        Widget(
            "watching",
            "Watching",
            "Pages and topics the office checks for you, and what it last told you.",
            "small",
            placed=True,
            agents_read=False,
            context=watches.widget_context,
        ),
        Widget(
            "delivered",
            "Delivered",
            "What the office finished this week, to have a look at.",
            "small",
            placed=True,
            agents_read=False,
            context=_delivered_context,
        ),
        Widget(
            "parked",
            "Parked for later",
            "Discussions you put off, coming back on a date.",
            "small",
            placed=True,
            agents_read=False,
            context=_parked_context,
        ),
        Widget(
            "month",
            "This month",
            "What the office spent this month and how much is under way.",
            "small",
            placed=True,
            agents_read=False,
            context=_month_context,
        ),
    )
}


def _widget(kind: str) -> Widget:
    widget = CATALOG.get(kind)
    if widget is None:
        raise WidgetRejected("widget_unknown")
    return widget


def _rows(session: Session) -> dict[str, OfficeWidget]:
    """Every widget's row, made with its defaults the first time it is read."""
    rows = {row.kind: row for row in session.scalars(select(OfficeWidget))}
    for index, widget in enumerate(CATALOG.values()):
        if widget.kind not in rows:
            row = OfficeWidget(
                kind=widget.kind,
                placed=widget.placed,
                position=index,
                size=widget.size,
                agents_read=widget.agents_read,
            )
            session.add(row)
            rows[widget.kind] = row
    session.flush()
    return rows


def _view(session: Session, row: OfficeWidget) -> dict[str, Any]:
    widget = CATALOG[row.kind]
    return {
        "kind": row.kind,
        "title": widget.title,
        "blurb": widget.blurb,
        "placed": row.placed,
        "position": row.position,
        "size": row.size,
        "default_size": widget.size,
        "agents_read": row.agents_read,
        "readable": widget.context is not None,
        "cleanable": widget.forget is not None,
        "held": widget.held(session),
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
    }


def wall(session: Session) -> list[dict[str, Any]]:
    """Every widget there is, placed ones first in their order."""
    rows = [row for row in _rows(session).values() if row.kind in CATALOG]
    rows.sort(key=lambda row: (not row.placed, row.position, row.kind))
    return [_view(session, row) for row in rows]


def placed(session: Session, kind: str) -> bool:
    """Whether a widget is on the wall; read without writing."""
    row = session.scalar(select(OfficeWidget).where(OfficeWidget.kind == kind))
    return CATALOG[kind].placed if row is None else bool(row.placed)


def place(session: Session, kind: str) -> dict[str, Any]:
    _widget(kind)
    rows = _rows(session)
    row = rows[kind]
    if not row.placed:
        row.placed = True
        row.removed_at = None
        row.position = 1 + max((r.position for r in rows.values() if r.placed), default=-1)
    session.flush()
    return _view(session, row)


def remove(session: Session, kind: str) -> dict[str, Any]:
    """Off the wall; what it knew stays until it is cleaned."""
    _widget(kind)
    row = _rows(session)[kind]
    row.placed = False
    row.removed_at = datetime.now(UTC)
    session.flush()
    return _view(session, row)


def clean(services: Services, kind: str) -> None:
    """Delete what the widget holds of its own: sign-ins, synced data, choices."""
    widget = _widget(kind)
    if widget.forget is not None:
        widget.forget(services)


def change(
    session: Session, kind: str, size: str | None = None, agents_read: bool | None = None
) -> dict[str, Any]:
    widget = _widget(kind)
    row = _rows(session)[kind]
    if size is not None:
        if size not in SIZES:
            raise WidgetRejected("widget_size_unknown")
        row.size = size
    if agents_read is not None:
        if agents_read and widget.context is None:
            raise WidgetRejected("widget_not_readable")
        row.agents_read = agents_read
    session.flush()
    return _view(session, row)


def arrange(session: Session, kinds: list[str]) -> list[dict[str, Any]]:
    """The placed widgets in the owner's order; any not named keep theirs after."""
    rows = _rows(session)
    named = [kind for kind in dict.fromkeys(kinds) if kind in rows]
    rest = sorted(
        (row for kind, row in rows.items() if kind not in named), key=lambda row: row.position
    )
    for index, row in enumerate([rows[kind] for kind in named] + rest):
        row.position = index
    session.flush()
    return wall(session)


def reads(session: Session, kind: str) -> bool:
    """Whether agents read this widget: on the wall, and allowed to."""
    row = session.scalar(select(OfficeWidget).where(OfficeWidget.kind == kind))
    widget = CATALOG[kind]
    if row is None:
        return widget.placed and widget.agents_read
    return bool(row.placed and row.agents_read)


def agent_context(
    session: Session, now: datetime, purpose: str, skip: tuple[str, ...] = ()
) -> list[str]:
    """What agents read from the wall, in the wall's order, within a budget.

    `skip` names widgets the caller tells agents about in its own way (the
    calendar has its own place in a task's context).
    """
    rows = list(session.scalars(select(OfficeWidget)))
    known = {row.kind: row for row in rows}
    order = sorted(
        CATALOG.values(),
        key=lambda w: known[w.kind].position if w.kind in known else 100,
    )
    parts: list[str] = []
    used = 0
    for widget in order:
        if widget.kind in skip or widget.context is None or not reads(session, widget.kind):
            continue
        try:
            text = widget.context(session, now, purpose).strip()
        except Exception:  # noqa: BLE001 - one widget must not stop the work
            logger.exception('{"event":"widget_context_failed","kind":"%s"}', widget.kind)
            continue
        if not text:
            continue
        room = CONTEXT_BUDGET - used
        if room <= 200:
            break
        text = text[:room]
        used += len(text)
        parts.append(text)
    return parts
