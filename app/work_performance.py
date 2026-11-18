"""How fast and how surely the office works.

Measured before anything is made faster, from what the office already
records: each step's start and end, each page it read (how, how long, or why
it failed), and why tasks failed. The benchmark is a fixed set of questions
the owner can run, so a change shows as a number rather than an impression.
Nothing here is sent anywhere.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Run, RunEvent, UsageEvent

# The questions the benchmark asks: the kinds of web work the owner hands the
# office (prices, rules, news, a page that needs a browser, a comparison).
BENCHMARK = (
    "What do the three largest Dutch banks charge per month for a basic business account?",
    "What are the current opening hours and ticket prices of the Rijksmuseum?",
    "Which EU AI Act obligations apply to a small company using a chatbot, and from when?",
    "Compare the list prices of the Pro plans of Notion, Asana and Monday.com.",
    "What changed in the latest stable release of Python, in five points?",
    "What is the Dutch VAT rate on restaurant meals and on takeaway food?",
    "Summarise this week's main news about ASML from at least two sources.",
    "What does Buienradar forecast for rain in Lyon over the next three hours?",
)
BENCHMARK_LABEL = "benchmark"
BENCHMARK_PROJECT = "Office benchmark"
STEP_LABEL = {
    "plan": "Planning",
    "replan": "Replanning",
    "research": "Research",
    "work": "Working through the plan",
    "strategy": "Analysis",
    "critique": "Review",
    "synthesis": "Writing the answer",
    "engineering": "Building",
}


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]


def step_times(session: Session, since: datetime) -> list[dict[str, Any]]:
    """Each kind of step: how many, typical and slow seconds."""
    rows = session.execute(
        select(RunEvent.run_id, RunEvent.kind, RunEvent.payload_json, RunEvent.created_at)
        .where(
            RunEvent.kind.in_(("step_started", "step_completed")),
            RunEvent.created_at >= since,
        )
        .order_by(RunEvent.id)
    ).all()
    began: dict[tuple[Any, str], datetime] = {}
    took: dict[str, list[float]] = defaultdict(list)
    for run_id, kind, payload, at in rows:
        node = str((payload or {}).get("node") or "")
        if not node:
            continue
        if kind == "step_started":
            began[(run_id, node)] = at
        elif (run_id, node) in began:
            took[node].append((at - began.pop((run_id, node))).total_seconds())
    return sorted(
        (
            {
                "step": node,
                "label": STEP_LABEL.get(node, node.replace("_", " ").capitalize()),
                "count": len(seconds),
                "typical_seconds": round(median(seconds)),
                "slow_seconds": round(_p90(seconds)),
            }
            for node, seconds in took.items()
            if seconds
        ),
        key=lambda row: -row["typical_seconds"],
    )


def page_reads(session: Session, since: datetime) -> dict[str, Any]:
    """Pages the office read: by which way, how long, and why some failed."""
    reads = [
        dict(payload or {})
        for (payload,) in session.execute(
            select(RunEvent.payload_json).where(
                RunEvent.kind == "page_read", RunEvent.created_at >= since
            )
        ).all()
    ]
    by_way: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for read in reads:
        by_way[str(read.get("via") or "direct")].append(read)
    ways = [
        {
            "via": way,
            "count": len(rows),
            "typical_ms": round(median([int(r.get("ms") or 0) for r in rows])),
            "typical_chars": round(median([int(r.get("chars") or 0) for r in rows])),
        }
        for way, rows in sorted(by_way.items(), key=lambda item: -len(item[1]))
    ]
    failed = [read for read in reads if not read.get("ok")]
    return {
        "count": len(reads),
        "read": len(reads) - len(failed),
        "ways": ways,
        "why_failed": [
            {"code": code, "count": count}
            for code, count in Counter(str(r.get("code") or "unknown") for r in failed).most_common(
                6
            )
        ],
    }


def failures(session: Session, since: datetime) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Run.error).where(Run.status == "FAILED", Run.completed_at >= since)
    ).all()
    counted = Counter(str(error or "unknown") for (error,) in rows)
    return [{"reason": reason, "count": count} for reason, count in counted.most_common(8)]


def run_cost(session: Session, run_ids: list[Any]) -> dict[Any, float]:
    costs: dict[Any, float] = defaultdict(float)
    if not run_ids:
        return costs
    for run_id, cost in session.execute(
        select(UsageEvent.run_id, UsageEvent.estimated_cost).where(UsageEvent.run_id.in_(run_ids))
    ).all():
        costs[run_id] += float(cost or 0.0)
    return costs


def tasks(session: Session, since: datetime) -> dict[str, Any]:
    """Finished tasks: how many, typical minutes and cost."""
    finished = list(
        session.scalars(select(Run).where(Run.status == "COMPLETED", Run.completed_at >= since))
    )
    minutes = [
        (run.completed_at - run.started_at).total_seconds() / 60
        for run in finished
        if run.completed_at and run.started_at
    ]
    costs = list(run_cost(session, [run.id for run in finished]).values())
    return {
        "finished": len(finished),
        "typical_minutes": round(median(minutes), 1) if minutes else None,
        "typical_cost_usd": round(median(costs), 3) if costs else None,
    }


def lead_patterns(session: Session, since: datetime) -> dict[str, Any]:
    """How the lead delegates: counts only, never what was asked or found."""
    rows = session.execute(
        select(RunEvent.run_id, RunEvent.kind, RunEvent.payload_json).where(
            RunEvent.kind.in_(("step_result", "lead_step")), RunEvent.created_at >= since
        )
    ).all()
    results = [
        (run_id, dict(payload or {})) for run_id, kind, payload in rows if kind == "step_result"
    ]
    calls = Counter(
        (run_id, payload["brief"])
        for run_id, kind, payload in rows
        if kind == "lead_step" and (payload or {}).get("brief")
    )
    per_task = Counter(run_id for run_id, _ in results)
    return {
        "tasks_delegating": len(per_task),
        "briefs": len(results),
        "typical_briefs": round(median(per_task.values())) if per_task else None,
        "typical_calls": round(median(calls.values())) if calls else None,
        "ended": dict(Counter(str(result.get("status") or "done") for _, result in results)),
        "fully_described": sum(bool(result.get("described")) for _, result in results),
        "addresses_taken_out": sum(len(result.get("unreturned") or []) for _, result in results),
    }


def _lead_checks(run: Run) -> dict[str, Any]:
    """What the code checked of a benchmark answer the lead gave."""
    lead = (run.state_json or {}).get("lead") or {}
    briefs = lead.get("results") or []
    return {
        "calls": lead.get("calls"),
        "unresolved": len(lead.get("unresolved") or []),
        "briefs": len(briefs),
        "unfinished": sum(brief.get("status") != "done" for brief in briefs),
    }


def benchmark_runs(session: Session) -> list[dict[str, Any]]:
    runs = list(
        session.scalars(
            select(Run)
            .where(Run.labels_json.contains([BENCHMARK_LABEL]))
            .order_by(Run.started_at.desc().nulls_last())
            .limit(len(BENCHMARK) * 3)
        )
    )
    costs = run_cost(session, [run.id for run in runs])
    return [
        {
            "run_id": str(run.id),
            "status": str(run.status),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "minutes": round((run.completed_at - run.started_at).total_seconds() / 60, 1)
            if run.completed_at and run.started_at
            else None,
            "cost_usd": round(costs.get(run.id, 0.0), 3),
            **_lead_checks(run),
        }
        for run in runs
    ]


def work_performance(
    session: Session, days: int = 30, now: datetime | None = None
) -> dict[str, Any]:
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    finished = tasks(session, since)
    per_task = finished["typical_cost_usd"]
    return {
        "days": days,
        "steps": step_times(session, since),
        "pages": page_reads(session, since),
        "failures": failures(session, since),
        "tasks": finished,
        "lead": lead_patterns(session, since),
        "benchmark": {
            "questions": list(BENCHMARK),
            "runs": benchmark_runs(session),
            # What a run of it may cost, from what tasks cost lately.
            "estimate_usd": round(per_task * len(BENCHMARK), 2) if per_task else None,
        },
    }
