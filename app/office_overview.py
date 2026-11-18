"""One read-only summary of the office for the Home and Tasks screens.

Everything here is derived from durable records: runs and their root tasks,
approvals, run events, usage events and chat usage. Nothing is estimated.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentProfile,
    Approval,
    ChannelUsage,
    Project,
    Run,
    RunEvent,
    Task,
    UsageEvent,
    Venture,
)
from app.titles import title_of

IN_MOTION = ("PENDING", "RUNNING", "RECOVERING", "PAUSED")
NEEDS_OWNER = ("WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "BLOCKED")
TASK_LIMIT = 100
ACTIVITY_LIMIT = 25


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _usage(session: Session, since: datetime) -> dict[str, Any]:
    runs = session.execute(
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.sum(UsageEvent.estimated_cost),
            func.count(UsageEvent.estimated_cost),
        ).where(UsageEvent.occurred_at >= since)
    ).one()
    chat = session.execute(
        select(
            func.coalesce(func.sum(ChannelUsage.calls), 0),
            func.coalesce(func.sum(ChannelUsage.tokens), 0),
            func.sum(ChannelUsage.estimated_cost),
        ).where(ChannelUsage.day >= since.date())
    ).one()
    calls, prompt, completion, cost, costed = runs
    cost_usd = float(cost or 0) + float(chat[2] or 0)
    return {
        "since": since.date().isoformat(),
        "calls": int(calls) + int(chat[0]),
        "tokens": int(prompt) + int(completion) + int(chat[1]),
        "cost_usd": round(cost_usd, 4),
        # Some providers report no price; then the cost is a lower bound.
        "cost_complete": int(costed) == int(calls),
    }


def office_overview(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    project_rows = list(session.scalars(select(Project)))
    projects = {row.id: row.name for row in project_rows}
    line_names = {row.id: row.name for row in session.scalars(select(Venture))}
    # The business line each project's tasks are judged by, to name on each task.
    lines = {row.id: line_names.get(row.venture_id) for row in project_rows if row.venture_id}
    latest_approval = (
        select(Approval.status)
        .where(Approval.run_id == Run.id)
        .order_by(Approval.requested_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Run, Task.objective, latest_approval, Task.title)
        .join(Task, Task.id == Run.root_task_id)
        .order_by(Run.started_at.desc().nulls_first(), Run.id)
        .limit(TASK_LIMIT)
    ).all()
    tasks = []
    for run, objective, approval_status, title in rows:
        origin = (run.config_snapshot_json or {}).get("origin") or {}
        tasks.append(
            {
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "project": projects.get(run.project_id, ""),
                "business_line": lines.get(run.project_id),
                "objective": objective,
                "title": title_of(title, objective),
                "status": run.status,
                "workflow": run.workflow_name,
                "current_node": run.current_node,
                "approval_status": approval_status,
                "error": run.error,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "requested_from": "chat" if origin else "office",
                "labels": list(run.labels_json or []),
            }
        )
    week_ago = now - timedelta(days=7)
    completed_week = session.scalar(
        select(func.count(Run.id)).where(Run.status == "COMPLETED", Run.completed_at >= week_ago)
    )
    completed_prior = session.scalar(
        select(func.count(Run.id)).where(
            Run.status == "COMPLETED",
            Run.completed_at >= week_ago - timedelta(days=7),
            Run.completed_at < week_ago,
        )
    )
    counts: dict[str, int] = {
        str(status): int(count)
        for status, count in session.execute(
            select(Run.status, func.count(Run.id)).group_by(Run.status)
        ).all()
    }
    events = session.execute(
        select(RunEvent, Task.objective, Run.id, Task.title)
        .join(Run, Run.id == RunEvent.run_id)
        .join(Task, Task.id == Run.root_task_id)
        .order_by(RunEvent.id.desc())
        .limit(ACTIVITY_LIMIT)
    ).all()
    agents = [
        {"id": str(row.id), "name": row.name, "role": row.role}
        for row in session.scalars(
            select(AgentProfile).where(AgentProfile.status == "ACTIVE").order_by(AgentProfile.name)
        )
    ]
    return {
        "now": now,
        "counts": {
            "in_motion": sum(counts.get(s, 0) for s in IN_MOTION),
            "needs_owner": sum(counts.get(s, 0) for s in NEEDS_OWNER),
            "completed_week": int(completed_week or 0),
            "completed_prior_week": int(completed_prior or 0),
        },
        "usage_month": _usage(session, _month_start(now)),
        "tasks": tasks,
        "activity": [
            {
                "cursor": event.id,
                "run_id": str(run_id),
                "objective": objective,
                "title": title_of(title, objective),
                "kind": event.kind,
                "payload": event.payload_json or {},
                "created_at": event.created_at,
            }
            for event, objective, run_id, title in events
        ],
        "agents": agents,
    }
