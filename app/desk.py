"""My desk: what the office brings back, sorted.

Three kinds of thing come back to the owner, and each has one place:

- what needs their decision goes to **Needs you**, and only that: a proposal
  to approve, work stopped for review, a rule asking, a limit reached. Each
  says what happened, where it came from, and what is needed;
- finished work goes to **Delivered**, to glance at, with what it concluded;
- ideas have their own place, **Ideas** (`app/ideas.py`);
- discussions put off for later wait under **Parked**, and a discussion whose
  day has come is brought back to **Continue**, with its brief.

Progress stays with the work and its discussion; nothing here is a stream of
"still working on it".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.orm import Session, sessionmaker

from app import ideas, watches
from app.db.models import Project, RoomMessage, Run, Task, Topic
from app.rooms import _concluded, clip, waiting_where
from app.titles import title_of
from app.topics import listed, touch

DELIVERED_DAYS = 7
DELIVERED_SHOWN = 6
# A discussion brought back stays marked "back" on the desk for a day.
BACK_FOR = timedelta(days=1)

SAID = {
    "policy": ("is waiting for your word on a rule.", "Allow it, or refuse."),
    "budget": ("stopped at its limit and kept everything it did.", "Raise the limit, or stop it."),
    "stuck": ("is stuck and asks for your help.", "Give a hint, skip that part, or finish."),
    "approval": ("proposes something to be done.", "Read the proposal and decide."),
    "review": ("stopped for your review.", "Accept it, or say what to change."),
    # A coding agent's permission question; refused after ten minutes.
    "agent": ("has a coding agent asking you.", "Choose, or it is refused in ten minutes."),
}


def _origin(session: Session, run: Run) -> dict[str, Any]:
    """Where the work came from: the discussion it was asked in, else its project."""
    if run.topic_id is not None:
        topic = session.get(Topic, run.topic_id)
        if topic is not None:
            return {"kind": "topic", "id": str(topic.id), "title": topic.title}
    project = session.get(Project, run.project_id)
    return {
        "kind": "project",
        "id": str(run.project_id),
        "title": project.name if project is not None else "",
    }


def needs(session: Session) -> list[dict[str, Any]]:
    """Every decision waiting on the owner, in the office, now."""
    shown = []
    for question in waiting_where(session, true()):
        run = session.get(Run, UUID(question["run_id"]))
        if run is None:
            continue
        happened, needed = SAID.get(question["kind"], ("is waiting for you.", "Open it."))
        why = question.get("reason") or ""
        shown.append(
            {
                **question,
                "what": f"{question.get('task')} {happened}",
                "why": clip(why, 300) if why else "",
                "need": needed,
                "from": _origin(session, run),
            }
        )
    return shown


def delivered(session: Session, store: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """Work finished this week, newest first, with what it concluded."""

    since = (now or datetime.now(UTC)) - timedelta(days=DELIVERED_DAYS)
    rows = session.execute(
        select(Run, Task.objective, Task.title)
        .join(Task, Task.id == Run.root_task_id)
        .where(Run.status.in_(("COMPLETED", "CLOSED")), Run.completed_at >= since)
        .order_by(Run.completed_at.desc())
        .limit(DELIVERED_SHOWN)
    ).all()
    return [
        {
            "run_id": str(run.id),
            "title": title_of(title, objective),
            "concluded": _concluded(session, store, run),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "from": _origin(session, run),
        }
        for run, objective, title in rows
    ]


def parked(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Discussions coming back on a date. Ideas have their own place."""
    now = now or datetime.now(UTC)
    returning = [
        {"id": str(topic.id), "title": topic.title, "revisit_at": topic.revisit_at.isoformat()}
        for topic in session.scalars(
            select(Topic)
            .where(Topic.revisit_at.is_not(None), Topic.revisit_at > now)
            .order_by(Topic.revisit_at)
        )
        if topic.revisit_at is not None
    ]
    return {"returning": returning}


def continuing(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Discussions to pick up: any brought back today first, then the most recent."""
    now = now or datetime.now(UTC)
    # Put off until a date, it stays parked until then.
    shown = [
        topic
        for topic in listed(session, "open")
        if not topic.get("revisit_at") or datetime.fromisoformat(topic["revisit_at"]) <= now
    ]
    for topic in shown:
        returned = (topic.get("brief") or {}).get("returned_at")
        topic["back"] = bool(returned and datetime.fromisoformat(returned) > now - BACK_FOR)
    return sorted(shown, key=lambda t: not t["back"])


def desk(session: Session, store: Any) -> dict[str, Any]:
    return {
        "needs": needs(session),
        "continue": continuing(session),
        "delivered": delivered(session, store),
        "parked": parked(session),
        "ideas": ideas.board(session, datetime.now(UTC)),
        "watches": watches.listed(session),
    }


def returns_due(session_factory: sessionmaker[Session], now: datetime) -> int:
    """Bring back the discussions whose day has come, with where they stood."""
    brought = 0
    with session_factory() as session, session.begin():
        due = list(
            session.scalars(
                select(Topic).where(Topic.revisit_at.is_not(None), Topic.revisit_at <= now)
            )
        )
        for topic in due:
            brief = dict(topic.brief_json or {})
            if brief.get("where"):
                said = f"Back as you asked. Where we were: {brief['where']}"
                if brief.get("open"):
                    said += f" Open: {brief['open']}"
            else:
                last = session.scalar(
                    select(RoomMessage)
                    .where(RoomMessage.topic_id == topic.id)
                    .order_by(RoomMessage.id.desc())
                    .limit(1)
                )
                said = "Back as you asked." + (
                    f" Last said: {clip(last.body, 240)}" if last is not None else ""
                )
            session.add(
                RoomMessage(
                    project_id=topic.project_id,
                    topic_id=topic.id,
                    author_kind="office",
                    author_name="The office",
                    body=said,
                )
            )
            topic.revisit_at = None
            topic.status = "open"
            topic.brief_json = {**brief, "returned_at": now.isoformat()}
            touch(session, topic.id)
            brought += 1
    return brought
