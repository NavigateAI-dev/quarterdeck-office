"""A board of the office's work, and how the work is going.

Columns are the run states themselves, so a card's position is what the
office is really doing, never a label someone dragged. Every number here is
derived from durable records: runs, approvals, usage events and artifacts.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import Approval, Artifact, Project, Run, Task, UsageEvent
from app.titles import title_of

COLUMNS: list[tuple[str, str, tuple[str, ...], str]] = [
    ("queued", "To do", ("PENDING",), "Accepted and waiting for a free worker."),
    ("working", "In progress", ("RUNNING", "RECOVERING"), "Agents are working on it now."),
    ("paused", "Paused", ("PAUSED",), "You paused it; agents are waiting."),
    (
        "with_you",
        "Waiting for you",
        ("WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "BLOCKED"),
        "Stopped for your decision. Nothing has left the office.",
    ),
    ("done", "Done", ("COMPLETED",), "Finished; the result is in the task room."),
    (
        "closed",
        "Closed",
        ("FAILED", "REJECTED", "CLOSED", "CANCELLED"),
        "Stopped for good: failed, rejected, cancelled or closed by you.",
    ),
]
COLUMN_OF_STATUS = {status: key for key, _, statuses, _ in COLUMNS for status in statuses}
BOARD_LIMIT = 300
WEEKS = 8
# What makes a task heavy: parallel research, revisions, saved artifacts and
# the tokens it took. Stated plainly, so the banding can be argued with.
HEAVY = 3.0
STANDARD = 1.2


def _effort(state: dict[str, Any], tokens: int, artifacts: int) -> dict[str, Any]:
    subtasks = len(state.get("research_subtasks") or [])
    revisions = int(state.get("revision_count") or 0)
    sources = len((state.get("research") or {}).get("sources") or [])
    score = (
        min(tokens, 1_000_000) / 200_000
        + revisions * 0.8
        + max(subtasks - 1, 0) * 0.4
        + artifacts * 0.15
    )
    band = "heavy" if score >= HEAVY else "standard" if score >= STANDARD else "light"
    return {
        "band": band,
        "score": round(score, 2),
        "revisions": revisions,
        "subquestions": subtasks,
        "sources": sources,
        "artifacts": artifacts,
        "tokens": tokens,
    }


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max((end - start).total_seconds(), 0) / 60, 1)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def office_board(
    session: Session, now: datetime | None = None, weeks: int = WEEKS
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    projects = {row.id: row.name for row in session.scalars(select(Project))}
    runs = list(
        session.execute(
            select(Run, Task.objective, Task.title)
            .join(Task, Task.id == Run.root_task_id)
            .order_by(Run.started_at.desc().nulls_first(), Run.id)
            .limit(BOARD_LIMIT)
        ).all()
    )
    ids = [run.id for run, _, _ in runs]
    spend = (
        {
            row.run_id: row
            for row in session.execute(
                select(
                    UsageEvent.run_id,
                    func.coalesce(
                        func.sum(
                            func.coalesce(UsageEvent.prompt_tokens, 0)
                            + func.coalesce(UsageEvent.completion_tokens, 0)
                        ),
                        0,
                    ).label("tokens"),
                    func.sum(cast(UsageEvent.estimated_cost, Float)).label("cost"),
                )
                .where(UsageEvent.run_id.in_(ids))
                .group_by(UsageEvent.run_id)
            ).all()
        }
        if ids
        else {}
    )
    artifacts = (
        {
            run_id: int(count)
            for run_id, count in session.execute(
                select(Artifact.run_id, func.count(Artifact.id))
                .where(Artifact.run_id.in_(ids))
                .group_by(Artifact.run_id)
            ).all()
        }
        if ids
        else {}
    )
    approvals = (
        {
            row.run_id: row
            for row in session.execute(
                select(
                    Approval.run_id,
                    func.min(Approval.requested_at).label("requested_at"),
                    func.max(Approval.resolved_at).label("resolved_at"),
                    func.count(Approval.id).label("rounds"),
                )
                .where(Approval.run_id.in_(ids))
                .group_by(Approval.run_id)
            ).all()
        }
        if ids
        else {}
    )
    cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _, _, _ in COLUMNS}
    finished: list[dict[str, Any]] = []
    for run, objective, title in runs:
        usage = spend.get(run.id)
        tokens = int(getattr(usage, "tokens", 0) or 0)
        cost = float(getattr(usage, "cost", 0) or 0)
        approval = approvals.get(run.id)
        state = run.state_json or {}
        waiting = _minutes(
            getattr(approval, "requested_at", None),
            getattr(approval, "resolved_at", None)
            or (now if run.status == "WAITING_FOR_APPROVAL" else None),
        )
        card = {
            "run_id": str(run.id),
            "project": projects.get(run.project_id, ""),
            "objective": objective,
            "title": title_of(title, objective),
            "status": run.status,
            "workflow": run.workflow_name,
            "step": run.current_node,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "error": run.error,
            "requested_from": "chat"
            if (run.config_snapshot_json or {}).get("origin")
            else "office",
            "labels": list(run.labels_json or []),
            "cost_usd": round(cost, 4),
            "effort": _effort(state, tokens, artifacts.get(run.id, 0)),
            "minutes_running": _minutes(run.started_at, run.completed_at or now),
            "minutes_with_you": waiting,
            "approval_rounds": int(getattr(approval, "rounds", 0) or 0),
        }
        cards[COLUMN_OF_STATUS.get(run.status, "closed")].append(card)
        if run.completed_at is not None:
            finished.append(card)
    return {
        "now": now,
        "columns": [
            {"key": key, "title": title, "about": about, "cards": cards[key]}
            for key, title, _, about in COLUMNS
        ],
        "flow": _flow(session, now, weeks),
        "quality": _quality(finished),
    }


def _flow(session: Session, now: datetime, weeks: int) -> list[dict[str, Any]]:
    """Volume and velocity, week by week: what arrived, what finished, what it cost."""
    rows = []
    for index in range(weeks - 1, -1, -1):
        end = now - timedelta(days=7 * index)
        start = end - timedelta(days=7)
        started = session.scalar(
            select(func.count(Run.id)).where(Run.started_at >= start, Run.started_at < end)
        )
        completed = list(
            session.execute(
                select(Run.started_at, Run.completed_at).where(
                    Run.status == "COMPLETED", Run.completed_at >= start, Run.completed_at < end
                )
            ).all()
        )
        durations = [
            minutes
            for minutes in (_minutes(first, second) for first, second in completed)
            if minutes is not None
        ]
        cost = session.scalar(
            select(func.sum(UsageEvent.estimated_cost)).where(
                UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end
            )
        )
        tokens = session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(UsageEvent.prompt_tokens, 0)
                        + func.coalesce(UsageEvent.completion_tokens, 0)
                    ),
                    0,
                )
            ).where(UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end)
        )
        rows.append(
            {
                "week_ending": end.date().isoformat(),
                "started": int(started or 0),
                "completed": len(completed),
                "median_minutes": _median(durations),
                "cost_usd": round(float(cost or 0), 4),
                "tokens": int(tokens or 0),
            }
        )
    return rows


def _quality(finished: list[dict[str, Any]]) -> dict[str, Any]:
    """How well the work goes: first-pass rate, waiting time, cost per task."""
    done = [card for card in finished if card["status"] == "COMPLETED"]
    first_pass = [card for card in done if card["effort"]["revisions"] == 0]
    waits = [card["minutes_with_you"] for card in done if card["minutes_with_you"] is not None]
    working = [card["minutes_running"] for card in done if card["minutes_running"] is not None]
    priced = [card["cost_usd"] for card in done if card["cost_usd"]]
    bands = {"light": 0, "standard": 0, "heavy": 0}
    for card in finished:
        bands[card["effort"]["band"]] += 1
    return {
        "completed": len(done),
        "stopped": len(finished) - len(done),
        "first_pass_rate": round(len(first_pass) / len(done), 2) if done else None,
        "median_working_minutes": _median(working),
        "median_waiting_minutes": _median(waits),
        "median_cost_usd": _median(priced),
        "effort_bands": bands,
    }
