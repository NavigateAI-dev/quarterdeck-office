"""What was due on a day, and where the owner says it stands.

A follow-up comes from a calendar event that is over, an idea planned for the
day, or the owner's own word. Asking is a routine's work (`app/routines/run.py`);
this keeps the record and says what to ask and what to report.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.calendars.store import BEHIND, events_between, preferences, zone_of
from app.db.models import CalendarEvent, FollowUp, Idea, Schedule
from app.routines.service import QUIET

PRESET = "progress-check"
RECAPS = ("morning-recap", "evening-recap")
ACTIVE = ("open", "doing")
# These go in the next recap, once.
FINISHED = ("done", "dropped", "parked")
STATUSES = (*ACTIVE, "done", "dropped", "not_a_task")
# A day the office was asleep should not lose what fell due on it.
LOOK_BACK_DAYS = 2
# Asked this many times with no answer, it stops being asked.
UNANSWERED_LIMIT = 5
TITLE_CHARS = 500
ASKING = (
    "Above each to-do is its id. First use follow_up_update with status not_a_task for each "
    "calendar event that is plainly nothing to finish (a holiday, a family outing, a regular "
    "meeting), and name those on one line so I can correct you. Then ask me where the rest stand, "
    "as one short message I can answer in one go, in the language I use with you. Do not search "
    "the web or propose a task. If nothing is left to ask, reply with exactly "
    f"{QUIET} and nothing else."
)
RECAP_INTRO = (
    "To-dos the office follows for me, as I have told it. Give them a short section of their own "
    "in this recap, using only what is said here:"
)


class FollowUpRejected(ValueError):
    """A change that cannot be made as asked; the message says why."""


def _zone(session: Session) -> ZoneInfo:
    return zone_of(preferences(session).get("timezone"))


def today_of(session: Session, now: datetime) -> date:
    return now.astimezone(_zone(session)).date()


def _sees_calendar(session: Session) -> bool:
    """Agents read these; with sharing on "busy" a calendar title stays private."""
    return bool(preferences(session)["share"] != "busy")


def _close(row: FollowUp, status: str, now: datetime, note: str = "", via: str = "office") -> None:
    if status != row.status:
        row.finished_at = now if status in FINISHED else None
        row.reported_at = None
    row.status = status
    _note(row, status, note, via, now)


def _note(row: FollowUp, status: str, note: str, via: str, now: datetime) -> None:
    if note:
        row.progress = note
    row.notes_json = [
        *row.notes_json,
        {"at": now.isoformat(), "status": status, "note": note, "via": via},
    ]


def gather(session: Session, now: datetime) -> None:
    """Bring the record up to date with the calendar and the ideas. Safe to repeat."""
    zone = _zone(session)
    today = now.astimezone(zone).date()
    if _sees_calendar(session):
        _gather_calendar(session, now, zone, today)
    _gather_ideas(session, now, today)
    session.flush()


def _gather_calendar(session: Session, now: datetime, zone: ZoneInfo, today: date) -> None:
    for row in session.scalars(
        select(FollowUp).where(FollowUp.source == "calendar", FollowUp.status.in_(ACTIVE))
    ):
        _, source_id, uid = row.source_ref.split(":", 2)
        event = session.scalar(
            select(CalendarEvent).where(
                CalendarEvent.source_id == UUID(source_id), CalendarEvent.uid == uid
            )
        )
        if event is None:
            # Calendars keep this far back, so anything nearer was taken off it.
            if row.due_on >= (now - BEHIND + timedelta(days=2)).date():
                _close(row, "gone", now)
            continue
        row.title = event.title[:TITLE_CHARS]
        # Moved on the calendar. A later day the owner gave stays as it is.
        row.due_on = max(row.due_on, event.starts_at.astimezone(zone).date())
    start = datetime.combine(today - timedelta(days=LOOK_BACK_DAYS), time.min, zone)
    end = datetime.combine(today + timedelta(days=1), time.min, zone)
    declined = set(session.scalars(select(FollowUp.title).where(FollowUp.status == "not_a_task")))
    for event in events_between(session, start, end):
        if event.starts_at < start or (not event.all_day and event.ends_at > now):
            continue
        ref = f"calendar:{event.source_id}:{event.uid}"
        if session.scalar(select(FollowUp.id).where(FollowUp.source_ref == ref)) is not None:
            continue
        title = event.title[:TITLE_CHARS]
        session.add(
            FollowUp(
                source="calendar",
                source_ref=ref,
                title=title,
                due_on=event.starts_at.astimezone(zone).date(),
                status="not_a_task" if title in declined else "open",
                notes_json=[],
            )
        )
        session.flush()


def _gather_ideas(session: Session, now: datetime, today: date) -> None:
    for row in session.scalars(
        select(FollowUp).where(FollowUp.source == "idea", FollowUp.status.in_(ACTIVE))
    ):
        idea = session.get(Idea, UUID(row.source_ref.split(":", 1)[1]))
        if idea is None or idea.status == "someday":
            _close(row, "gone", now)
        elif idea.status == "dropped":
            _close(row, "dropped", now)
        elif idea.status != "open":
            _close(row, "done", now)
        elif idea.plan_for is None:
            _close(row, "gone", now)
        else:
            row.due_on = max(row.due_on, idea.plan_for)
    for idea in session.scalars(
        select(Idea).where(
            Idea.status == "open", Idea.plan_for.is_not(None), Idea.plan_for <= today
        )
    ):
        ref = f"idea:{idea.id}"
        if session.scalar(select(FollowUp.id).where(FollowUp.source_ref == ref)) is None:
            session.add(
                FollowUp(
                    source="idea",
                    source_ref=ref,
                    title=idea.text[:TITLE_CHARS],
                    due_on=idea.plan_for,
                    notes_json=[],
                )
            )
            session.flush()


def add(session: Session, title: str, due_on: date, now: datetime, via: str = "office") -> FollowUp:
    """One the owner asked to be followed, with no event or idea behind it."""
    row = FollowUp(
        source="owner",
        source_ref=f"owner:{int(now.timestamp() * 1000)}:{title[:40]}",
        title=title[:TITLE_CHARS],
        due_on=due_on,
        notes_json=[],
    )
    session.add(row)
    session.flush()
    _note(row, "open", "", via, now)
    return row


def found(session: Session, follow_up_id: UUID) -> FollowUp:
    row = session.get(FollowUp, follow_up_id)
    if row is None:
        raise FollowUpRejected("no such to-do")
    return row


def record(
    session: Session,
    row: FollowUp,
    now: datetime,
    *,
    status: str | None = None,
    note: str = "",
    next_on: date | None = None,
    via: str = "office",
) -> FollowUp:
    """What the owner said about it. Any word from them starts the count of asks again."""
    if status is not None and status not in STATUSES:
        raise FollowUpRejected(f"status must be one of {', '.join(STATUSES)}")
    if next_on is not None and next_on < today_of(session, now):
        raise FollowUpRejected("that day has passed")
    was = row.status
    if status is None:
        status = "doing" if was == "open" else was
    if was == "not_a_task" and status != was:
        # The owner wants it asked about after all: forget the title.
        session.execute(
            update(FollowUp)
            .where(
                FollowUp.title == row.title, FollowUp.status == "not_a_task", FollowUp.id != row.id
            )
            .values(status="gone")
        )
    if status in ("done", "dropped") and was not in FINISHED and row.source == "idea":
        idea = session.get(Idea, UUID(row.source_ref.split(":", 1)[1]))
        if idea is not None and idea.status in ("open", "someday"):
            idea.status = status
    row.asked_count = 0
    if next_on is not None:
        row.due_on = next_on
    _close(row, status, now, note.strip(), via)
    session.flush()
    return row


def park_unanswered(session: Session, now: datetime) -> None:
    for row in session.scalars(
        select(FollowUp).where(
            FollowUp.status.in_(ACTIVE), FollowUp.asked_count >= UNANSWERED_LIMIT
        )
    ):
        _close(row, "parked", now)


def _visible(session: Session, statement: Any) -> list[FollowUp]:
    if not _sees_calendar(session):
        statement = statement.where(FollowUp.source != "calendar")
    return list(session.scalars(statement.order_by(FollowUp.due_on, FollowUp.created_at)))


def due_now(session: Session, today: date) -> list[FollowUp]:
    return _visible(
        session, select(FollowUp).where(FollowUp.status.in_(ACTIVE), FollowUp.due_on <= today)
    )


def _line(row: FollowUp, with_id: bool = True) -> str:
    start = f"- {row.id}: " if with_id else "- "
    heard = f"; last heard: {row.progress}" if row.progress else "; nothing heard yet"
    return f"{start}{row.title} ({row.source}, due {row.due_on:%a %d %b}{heard})"


def check_in(session: Session, row: Schedule, now: datetime) -> str | None:
    """The message a Progress check asks with, or None when nothing is due."""
    gather(session, now)
    park_unanswered(session, now)
    today = today_of(session, now)
    rows = due_now(session, today)
    if not rows:
        return None
    for item in rows:
        if item.last_asked_on != today:
            item.asked_count += 1
            item.last_asked_on = today
    items = "\n".join(_line(item) for item in rows)
    return (
        f"{row.title} (a routine, not typed by me):\n{row.prompt}\n\n"
        f"Today is {today:%A %d %B %Y}. The to-dos:\n{items}\n\n{ASKING}"
    )


def recap_words(session: Session, now: datetime) -> str:
    """What a recap should say about them; empty when there is nothing."""
    gather(session, now)
    finished = _visible(
        session,
        select(FollowUp).where(FollowUp.status.in_(FINISHED), FollowUp.reported_at.is_(None)),
    )
    still = due_now(session, today_of(session, now))
    if not finished and not still:
        return ""
    parts = [RECAP_INTRO]
    if finished:
        parts.append("Finished since the last recap:")
        parts += [
            f"- {row.title} ({row.status}" + (f": {row.progress}" if row.progress else "") + ")"
            for row in finished
        ]
    if still:
        parts.append("Still open:")
        parts += [_line(row, with_id=False) for row in still]
    return "\n".join(parts)


def mark_reported(session: Session, until: datetime, now: datetime) -> None:
    """A recap went out, with everything finished by the time it was asked for."""
    session.execute(
        update(FollowUp)
        .where(
            FollowUp.status.in_(FINISHED),
            FollowUp.reported_at.is_(None),
            FollowUp.finished_at <= until,
        )
        .values(reported_at=now)
    )


def told(session: Session, now: datetime) -> str:
    """What an agent is told, so an answer in passing reaches the record."""
    rows = due_now(session, today_of(session, now))
    head = (
        "The office follows the owner's to-dos and asks about them each day. When the owner says "
        "where one stands, record it with follow_up_update, and say what you recorded."
    )
    return "\n".join([head, *(_line(row) for row in rows)])


def view(row: FollowUp) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "source": row.source,
        "due_on": row.due_on.isoformat(),
        "status": row.status,
        "progress": row.progress,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def listed(session: Session, now: datetime) -> dict[str, list[dict[str, Any]]]:
    """For the page: open ones, and what finished in the last week."""
    since = (now if now.tzinfo else now.replace(tzinfo=UTC)) - timedelta(days=7)
    return {
        "open": [
            view(row)
            for row in _visible(session, select(FollowUp).where(FollowUp.status.in_(ACTIVE)))
        ],
        "finished": [
            view(row)
            for row in _visible(
                session,
                select(FollowUp).where(
                    FollowUp.status.in_(FINISHED), FollowUp.finished_at >= since
                ),
            )
        ],
    }
