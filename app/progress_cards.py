"""The figures behind the progress cards for projects and tasks.

A project has no deadline; what runs out is its month against its spending
limit, so its timeline is the month and its forecast is where the spend ends at
the current pace. A task's timeline is its time limit, and its forecast is when
it finishes at the pace its steps have kept so far. A forecast is given only
when there is enough to forecast from.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Project, Run, Task, UsageEvent, Venture
from app.office_pulse import RUNNING, WAITING, WAITING_FOR, budget, progress
from app.project_settings import spent_this_month as spent_in_project
from app.titles import title_of

# Below this share of the month, a forecast would multiply one task's spend.
EARLIEST_FORECAST = 0.1
ACCENTS = 4
RECENT_TASKS = 6
NEXT_ITEMS = 3
LABEL_CHARS = 36
FINISHED = ("COMPLETED",)
NOT_COUNTED = ("CANCELLED", "CLOSED", "REJECTED")
NEEDS_YOU = ("WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "BLOCKED", "PAUSED")


def accent(key: object) -> int:
    """A stable colour for a key; projects get theirs from `accents`."""
    return int(hashlib.sha256(str(key).encode()).hexdigest(), 16) % ACCENTS


def accents(session: Session) -> dict[UUID, int]:
    """Each project's colour, in the order the projects were made, so that
    neighbours differ; its tasks wear the same one."""
    made = session.scalars(select(Project.id).order_by(Project.created_at, Project.id))
    return {project_id: index % ACCENTS for index, project_id in enumerate(made)}


def _month(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def month_forecast(spent: float, limit: float | None, now: datetime) -> dict[str, Any]:
    """Where this month's spend ends at its current pace, if that can be said yet."""
    start, end = _month(now)
    elapsed = round((now - start) / (end - start), 4)
    forecast = round(spent / elapsed, 2) if elapsed >= EARLIEST_FORECAST else None
    return {
        "spent": round(spent, 4),
        "limit": limit,
        "share": round(spent / limit, 4) if limit else None,
        "elapsed": elapsed,
        "forecast": forecast,
        "forecast_share": round(forecast / limit, 4) if forecast is not None and limit else None,
        "month_start": start.date().isoformat(),
        "month_end": (end - timedelta(days=1)).date().isoformat(),
    }


def task_pace(
    done: int, total: int, elapsed_minutes: float, max_minutes: float | None
) -> dict[str, Any]:
    """When a task finishes at the pace its steps have kept, and whether that
    is past its time limit."""
    if done <= 0 or total <= 0:
        return {"remaining_minutes": None, "over_limit_by": None}
    remaining = round(elapsed_minutes / done * max(total - done, 0), 1)
    finish = elapsed_minutes + remaining
    over = round(finish - max_minutes, 1) if max_minutes and finish > max_minutes else None
    return {"remaining_minutes": remaining, "over_limit_by": over}


def step_label(step: dict[str, Any]) -> str:
    """A step named by the first clause of its brief, short enough for a row."""
    brief = str(step.get("brief") or "").strip()
    first = re.split(r"[，,。；;：:\n]|(?<=[a-z])\. ", brief, maxsplit=1)[0].strip()
    if not first:
        first = str(step.get("id") or "step").replace("_", " ").replace("-", " ").strip()
        first = first[:1].upper() + first[1:]
    if len(first) > LABEL_CHARS:
        first = first[: LABEL_CHARS - 1].rstrip() + "…"
    return first


def _spend_by_run(session: Session, run_ids: list[UUID]) -> dict[UUID, tuple[float, int]]:
    if not run_ids:
        return {}
    rows = session.execute(
        select(
            UsageEvent.run_id,
            func.coalesce(func.sum(cast(UsageEvent.estimated_cost, Float)), 0.0),
            func.coalesce(
                func.sum(
                    func.coalesce(UsageEvent.prompt_tokens, 0)
                    + func.coalesce(UsageEvent.completion_tokens, 0)
                ),
                0,
            ),
        )
        .where(UsageEvent.run_id.in_(run_ids))
        .group_by(UsageEvent.run_id)
    )
    return {run_id: (float(cost or 0), int(tokens or 0)) for run_id, cost, tokens in rows}


def project_cards(
    session: Session, now: datetime, colours: dict[UUID, int] | None = None
) -> list[dict[str, Any]]:
    colours = colours if colours is not None else accents(session)
    projects = list(
        session.scalars(select(Project).where(Project.status == "ACTIVE").order_by(Project.name))
    )
    lines = {row.id: row.name for row in session.scalars(select(Venture))}
    by_project: dict[UUID, list[tuple[Run, str, str | None]]] = defaultdict(list)
    for run, objective, title in session.execute(
        select(Run, Task.objective, Task.title)
        .join(Task, Task.id == Run.root_task_id)
        .where(Run.project_id.in_([p.id for p in projects]))
        .order_by(Run.started_at.desc().nulls_last())
    ):
        by_project[run.project_id].append((run, objective, title))
    cards = []
    for project in projects:
        runs = by_project.get(project.id, [])
        counted = [run for run, _, _ in runs if run.status not in NOT_COUNTED]
        done = sum(1 for run in counted if run.status in FINISHED)
        running = sum(1 for run in counted if run.status in RUNNING)
        needs = [(r, o, t) for r, o, t in runs if r.status in NEEDS_YOU]
        last = max(
            (
                moment
                for run, _, _ in runs
                for moment in (run.completed_at, run.started_at)
                if moment
            ),
            default=None,
        )
        cards.append(
            {
                "id": str(project.id),
                "name": project.name,
                "line": lines.get(project.venture_id) if project.venture_id else None,
                "accent": colours.get(project.id, accent(project.id)),
                "state": "needs_you" if needs else "working" if running else "quiet",
                "counts": {
                    "total": len(counted),
                    "done": done,
                    "running": running,
                    "needs_you": len(needs),
                    "failed": sum(1 for run in counted if run.status == "FAILED"),
                },
                "ring": round(done / len(counted), 4) if counted else None,
                "month": month_forecast(
                    spent_in_project(session, project.id, now),
                    project.monthly_cost_limit_usd,
                    now,
                ),
                "tasks": [
                    {
                        "run_id": str(run.id),
                        "title": title_of(title, objective),
                        "status": run.status,
                        **progress(run),
                    }
                    for run, objective, title in runs[:RECENT_TASKS]
                ],
                "next": [
                    {
                        "run_id": str(run.id),
                        "title": title_of(title, objective),
                        "reason": WAITING_FOR.get(run.status, run.status),
                    }
                    for run, objective, title in needs[:NEXT_ITEMS]
                ],
                "last_activity": last.isoformat() if last else None,
            }
        )
    return cards


def _steps(run: Run) -> list[dict[str, Any]]:
    state = dict(run.state_json or {})
    status = state.get("step_status") or {}
    planned = [
        step
        for step in ((state.get("plan") or {}).get("steps") or [])
        if isinstance(step, dict) and step.get("id")
    ]
    return [
        {
            "id": str(step["id"]),
            "label": step_label(step),
            "kind": step.get("kind") or "research",
            "status": status.get(step["id"], "pending"),
        }
        for step in planned
    ]


def task_cards(
    session: Session, now: datetime, colours: dict[UUID, int] | None = None
) -> list[dict[str, Any]]:
    colours = colours if colours is not None else accents(session)
    rows = session.execute(
        select(Run, Task.objective, Task.title, Project.name)
        .join(Task, Task.id == Run.root_task_id)
        .join(Project, Project.id == Run.project_id)
        .where(Run.status.in_(RUNNING + WAITING))
        .order_by(Run.started_at.desc().nulls_last())
    ).all()
    spend = _spend_by_run(session, [run.id for run, *_ in rows])
    cards = []
    for run, objective, title, project_name in rows:
        cost, tokens = spend.get(run.id, (0.0, 0))
        moved = progress(run)
        started = run.started_at
        elapsed = round((now - started).total_seconds() / 60, 1) if started else 0.0
        max_minutes = (run.budget_json or {}).get("max_minutes")
        steps = _steps(run)
        cards.append(
            {
                "run_id": str(run.id),
                "title": title_of(title, objective),
                "project": project_name,
                "accent": colours.get(run.project_id, accent(run.project_id)),
                "status": run.status,
                "reason": WAITING_FOR.get(run.status) if run.status in WAITING else None,
                "progress": moved,
                "ring": round(moved["done"] / moved["total"], 4) if moved["total"] else None,
                "budget": budget(run, cost, tokens),
                "elapsed_minutes": elapsed,
                "max_minutes": max_minutes,
                "pace": task_pace(moved["done"], moved["total"], elapsed, max_minutes)
                if run.status in RUNNING
                else {"remaining_minutes": None, "over_limit_by": None},
                "steps": steps,
                "next": [step["label"] for step in steps if step["status"] == "pending"][
                    :NEXT_ITEMS
                ],
                "started_at": started.isoformat() if started else None,
            }
        )
    return cards


def office_progress(
    session: Session, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    colours = accents(session)
    return {
        "projects": project_cards(session, now, colours),
        "tasks": task_cards(session, now, colours),
    }
