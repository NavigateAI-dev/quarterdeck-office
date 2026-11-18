"""The office at a glance: progress, spend, and what needs the owner.

After Codenotch's notch: a ring for how far each running task has come, a ring
for how much of its budget it has used, what waits on the owner, what just
finished, and the month's spend against its limits. Limits warn at 80% and at
100%, once per crossing and window, and the warning is recorded so it is never
repeated and a new month starts fresh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Float, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import LimitAlert, Project, Run, Task, UsageEvent
from app.office_settings import effective, spent_this_month
from app.project_settings import spent_this_month as spent_in_project
from app.titles import title_of

RUNNING = ("PENDING", "RUNNING", "RECOVERING")
WAITING = ("WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "BLOCKED", "PAUSED")
FINISHED = ("COMPLETED", "FAILED", "CANCELLED", "REJECTED", "CLOSED")
THRESHOLDS = (80, 100)
FINISHED_WITHIN = timedelta(hours=24)
ALERTS_WITHIN = timedelta(days=7)
# The fixed pipeline's stages, for a task planned without steps.
STAGES = {
    "agent_research": ("plan", "research", "critique", "synthesis"),
}
DEFAULT_STAGES = ("plan", "research", "strategy", "critique", "synthesis")
# A step plan's work counts as research for a task measured by stages.
AS_STAGE = {"work": "research", "replan": "research"}
REVIEWED = ("critique", "synthesis", "approval", "engineering")
ANSWERED = ("synthesis", "approval", "engineering")
WAITING_FOR = {
    "WAITING_FOR_APPROVAL": "Waiting for your approval",
    "NEEDS_HUMAN_REVIEW": "Needs your review",
    "BLOCKED": "Blocked by the critic",
    "PAUSED": "Paused",
}


def progress(run: Run) -> dict[str, Any]:
    """How far a task has come: its plan's steps, or the pipeline's stages."""
    state = dict(run.state_json or {})
    steps = [
        step for step in ((state.get("plan") or {}).get("steps") or []) if isinstance(step, dict)
    ]
    node = run.current_node or ""
    if steps:
        status = state.get("step_status") or {}
        done = sum(1 for step in steps if status.get(step.get("id")) == "done")
        done += int(node in REVIEWED) + int(node in ANSWERED)
        return {"done": done, "total": len(steps) + 2, "unit": "steps"}
    stages = STAGES.get(run.workflow_name, DEFAULT_STAGES)
    node = AS_STAGE.get(node, node)
    done = stages.index(node) + 1 if node in stages else 0
    return {"done": done, "total": len(stages), "unit": "stages"}


def budget(run: Run, cost: float, tokens: int) -> dict[str, Any]:
    """How much of its own ceiling a task has used: the nearer of cost and tokens."""
    limits = dict(run.budget_json or {})
    max_cost = limits.get("max_cost_usd")
    max_tokens = limits.get("max_tokens")
    shares = {}
    if isinstance(max_cost, int | float) and max_cost > 0:
        shares["cost"] = cost / float(max_cost)
    if isinstance(max_tokens, int | float) and max_tokens > 0:
        shares["tokens"] = tokens / float(max_tokens)
    limit = max(shares, key=lambda key: shares[key]) if shares else None
    return {
        "share": round(shares[limit], 4) if limit else None,
        "limit": limit,
        "spent_cost": round(cost, 4),
        "max_cost": max_cost,
        "spent_tokens": tokens,
        "max_tokens": max_tokens,
    }


Crossing = tuple[str, str, str, str, float]


def _crossings(
    window: str,
    month: dict[str, Any],
    project_limits: list[dict[str, Any]],
    running: list[dict[str, Any]],
) -> list[Crossing]:
    found: list[Crossing] = []
    if month["share"] is not None:
        found.append(("office", "office", "This month's office limit", window, month["share"]))
    for entry in project_limits:
        if entry["share"] is not None:
            found.append(("project", entry["project_id"], entry["name"], window, entry["share"]))
    for entry in running:
        if entry["budget"]["share"] is not None:
            found.append(("task", entry["run_id"], entry["title"], "run", entry["budget"]["share"]))
    return found


def _record(session: Session, crossings: list[Crossing]) -> None:
    """Each limit passed, once per threshold and window."""
    for scope, subject, name, span, share in crossings:
        for threshold in THRESHOLDS:
            if share * 100 >= threshold:
                session.execute(
                    insert(LimitAlert)
                    .values(
                        scope=scope,
                        subject=subject,
                        name=name[:255],
                        window=span,
                        threshold=threshold,
                        share=share,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["scope", "subject", "window", "threshold"]
                    )
                )


def record_crossings(session: Session, settings: Settings, now: datetime | None = None) -> None:
    """Record the limits passed now, with or without anyone looking.

    The pulse records them when the office is open; the scheduler calls this
    so a limit crossed with the office closed is still recorded, and announced.
    """
    pulse(session, settings, now)


def pulse(session: Session, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    projects = {row.id: row for row in session.scalars(select(Project))}
    rows = session.execute(
        select(Run, Task.objective, Task.title)
        .join(Task, Task.id == Run.root_task_id)
        .where(Run.status.in_(RUNNING + WAITING))
    ).all()
    live_ids = [run.id for run, _, _ in rows]
    spent: dict[UUID, tuple[float, int]] = {}
    if live_ids:
        for run_id, cost, tokens in session.execute(
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
            .where(UsageEvent.run_id.in_(live_ids))
            .group_by(UsageEvent.run_id)
        ):
            spent[run_id] = (float(cost or 0), int(tokens or 0))

    running: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for run, objective, title in rows:
        project = projects.get(run.project_id)
        base = {
            "run_id": str(run.id),
            "title": title_of(title, objective),
            "project": project.name if project else "",
        }
        cost, tokens = spent.get(run.id, (0.0, 0))
        if run.status in RUNNING:
            running.append(
                base
                | {
                    "status": run.status,
                    "step": run.current_node,
                    "progress": progress(run),
                    "budget": budget(run, cost, tokens),
                }
            )
        else:
            waiting.append(
                base | {"status": run.status, "reason": WAITING_FOR.get(run.status, run.status)}
            )

    finished = [
        {
            "run_id": str(run.id),
            "title": title_of(title, objective),
            "status": run.status,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }
        for run, objective, title in session.execute(
            select(Run, Task.objective, Task.title)
            .join(Task, Task.id == Run.root_task_id)
            .where(Run.status.in_(FINISHED), Run.completed_at >= now - FINISHED_WITHIN)
            .order_by(Run.completed_at.desc())
            .limit(10)
        ).all()
    ]

    window = now.strftime("%Y-%m")
    limit = effective(session, settings).get("monthly_cost_limit_usd")
    month_spent = spent_this_month(session, now)
    month: dict[str, Any] = {
        "spent": round(month_spent, 4),
        "limit": limit,
        "share": round(month_spent / limit, 4) if limit else None,
    }
    project_limits: list[dict[str, Any]] = []
    for project in projects.values():
        if project.monthly_cost_limit_usd is None or project.status != "ACTIVE":
            continue
        used = spent_in_project(session, project.id, now)
        project_limits.append(
            {
                "project_id": str(project.id),
                "name": project.name,
                "spent": round(used, 4),
                "limit": project.monthly_cost_limit_usd,
                "share": round(used / project.monthly_cost_limit_usd, 4)
                if project.monthly_cost_limit_usd
                else None,
            }
        )

    _record(session, _crossings(window, month, project_limits, running))
    alerts = [
        {
            "id": str(row.id),
            "scope": row.scope,
            "subject": row.subject,
            "name": row.name,
            "threshold": row.threshold,
            "share": row.share,
            "window": row.window,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in session.scalars(
            select(LimitAlert)
            .where(LimitAlert.created_at >= now - ALERTS_WITHIN)
            .order_by(LimitAlert.created_at.desc())
            .limit(20)
        )
    ]
    return {
        "running": running,
        "waiting": waiting,
        "finished": finished,
        "month": month,
        "projects": project_limits,
        "alerts": alerts,
    }
