"""The sidebar: business spaces, each with its threads and their state.

One read for the whole sidebar, instead of the separate task, room and
discussion lists it polled before. A thread is a discussion (a topic), or a
task asked without one whose project has no home discussion to show it in.

Every thread has one of four states, the same word everywhere it shows:
needs (waiting on the owner, with what for), working, done (finished in the
last day) and idle; and failed for work that stopped on an error. A space
shows everything that needs the owner or is working, then its five most recent
other threads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from app.db.models import Project, Run, Task, Topic, Venture
from app.rooms import waiting_where
from app.titles import title_of
from app.topics import runs_of

IDLE_SHOWN = 5
DONE_FOR = timedelta(days=1)
# Tasks asked outside a discussion are listed for this long.
TASKS_FOR = timedelta(days=14)
NO_SPACE = "No space yet"
BADGE = {
    "policy": "Rule",
    "approval": "Approval",
    "review": "Review",
    "stuck": "Stuck",
    "budget": "Budget",
    "agent": "Agent asks",
}
WORKING = ("QUEUED", "RUNNING", "PENDING")
FAILED = ("FAILED",)
DONE = ("COMPLETED", "CLOSED")
ORDER = {"needs": 0, "working": 1}


def sidebar(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    waiting = waiting_where(session, true())
    needs = {question["run_id"]: question for question in waiting}
    ventures = {row.id: row.name for row in session.scalars(select(Venture))}
    projects = {row.id: row for row in session.scalars(select(Project))}
    homed = {
        topic.project_id
        for topic in session.scalars(select(Topic).where(Topic.home.is_(True)))
        if topic.project_id is not None
    }
    threads: list[dict[str, Any]] = []
    for topic in session.scalars(select(Topic).where(Topic.status == "open")):
        runs = list(session.scalars(select(Run).where(runs_of(topic))))
        state, badge = _state(runs, needs, now)
        project = projects.get(topic.project_id) if topic.project_id else None
        threads.append(
            {
                "kind": "topic",
                "id": str(topic.id),
                "title": topic.title,
                "href": f"#topic/{topic.id}",
                "state": state,
                "badge": badge,
                "at": _iso(topic.last_activity_at),
                "space": topic.venture_id or (project.venture_id if project else None),
            }
        )
    loose = session.execute(
        select(Run, Task.objective, Task.title, Task.created_at)
        .join(Task, Task.id == Run.root_task_id)
        .where(Run.topic_id.is_(None), Task.created_at >= now - TASKS_FOR)
        .order_by(Task.created_at.desc())
    ).all()
    for run, objective, title, asked_at in loose:
        if run.project_id in homed:
            continue  # its project's home discussion shows it
        state, badge = _state([run], needs, now)
        project = projects.get(run.project_id)
        threads.append(
            {
                "kind": "task",
                "id": str(run.id),
                "title": title_of(title, objective),
                "href": f"#task/{run.id}",
                "state": state,
                "badge": badge,
                "at": _iso(run.completed_at or run.started_at or asked_at),
                "space": project.venture_id if project else None,
            }
        )
    return {"spaces": _spaces(threads, ventures), "needs": len(waiting)}


def _state(
    runs: list[Run], needs: dict[str, dict[str, Any]], now: datetime
) -> tuple[str, str | None]:
    for run in runs:
        question = needs.get(str(run.id))
        if question is not None:
            return "needs", BADGE.get(str(question["kind"]), "Needs you")
    if any(run.status in WORKING for run in runs):
        return "working", None
    latest = max(runs, key=_when, default=None)
    if latest is not None and latest.status in FAILED:
        return "failed", None
    if (
        latest is not None
        and latest.status in DONE
        and latest.completed_at is not None
        and latest.completed_at >= now - DONE_FOR
    ):
        return "done", None
    return "idle", None


def _spaces(threads: list[dict[str, Any]], ventures: dict[UUID, str]) -> list[dict[str, Any]]:
    grouped: dict[UUID | None, list[dict[str, Any]]] = {}
    for thread in threads:
        grouped.setdefault(thread.pop("space"), []).append(thread)
    spaces = []
    for space_id, items in grouped.items():
        # Newest first, then (a stable sort) what needs the owner, then what works.
        items.sort(key=lambda t: t["at"] or "", reverse=True)
        items.sort(key=lambda t: ORDER.get(t["state"], 2))
        live = [t for t in items if t["state"] in ORDER]
        rest = [t for t in items if t["state"] not in ORDER]
        spaces.append(
            {
                "id": str(space_id) if space_id else None,
                "name": ventures.get(space_id, NO_SPACE) if space_id else NO_SPACE,
                "threads": live + rest[:IDLE_SHOWN],
                "more": max(0, len(rest) - IDLE_SHOWN),
            }
        )
    # Spaces with something for the owner first, then by name; no space last.
    return sorted(
        spaces,
        key=lambda s: (
            s["id"] is None,
            not any(t["state"] == "needs" for t in s["threads"]),
            s["name"].lower(),
        ),
    )


def _when(run: Run) -> datetime:
    return run.completed_at or run.started_at or datetime.min.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
