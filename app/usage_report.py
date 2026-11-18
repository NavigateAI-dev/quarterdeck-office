"""Token use, cost and model performance, from the durable usage records."""

import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import ChannelUsage, ModelConnection, UsageEvent


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


def _rank(values: list[float], fraction: float) -> float:
    """The nearest-rank percentile: a time a call actually took, not a blend."""
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 2)


def model_health(session: Session, since: datetime) -> list[dict[str, Any]]:
    """Per model: how reliable, how fast and how slow at worst."""
    model = func.coalesce(UsageEvent.resolved_model, UsageEvent.model_alias)
    # A failed call through a connection never learns the model's own name, so
    # it is named by its connection; the two are one model and one row.
    through = {
        f"connection:{row.id}": row.model_id for row in session.scalars(select(ModelConnection))
    }
    calls: dict[str, list[tuple[str, float, int | None, str | None]]] = {}
    for key, status, latency, output, error in session.execute(
        select(
            model,
            UsageEvent.status,
            UsageEvent.latency_seconds,
            UsageEvent.completion_tokens,
            UsageEvent.error_code,
        ).where(
            UsageEvent.occurred_at >= since,
            # A budget stop never reached a model: not the model's health.
            func.coalesce(UsageEvent.error_code, "").not_like("budget_exhausted%"),
        )
    ).all():
        name = str(key or "unknown")
        calls.setdefault(through.get(name, name), []).append(
            (str(status or ""), float(latency or 0), output, error)
        )
    health = []
    for key, rows in calls.items():
        ok = [row for row in rows if row[0].lower() == "ok"]
        failed = [row for row in rows if row[0].lower() != "ok"]
        answered = [(latency, output) for _, latency, output, _ in ok if output and latency > 0]
        errors = Counter(error or status for status, _, _, error in failed)
        latencies = [latency for _, latency, _, _ in rows]
        health.append(
            {
                "model": key,
                "calls": len(rows),
                "failures": len(failed),
                "reliability": round(len(ok) / len(rows), 4),
                "common_failure": errors.most_common(1)[0][0] if errors else None,
                "p50_seconds": _rank(latencies, 0.5),
                "p95_seconds": _rank(latencies, 0.95),
                # Output tokens over answering time, where both are known;
                # none rather than a zero for a model that never answered.
                "tokens_per_second": round(
                    sum(output for _, output in answered) / sum(latency for latency, _ in answered),
                    1,
                )
                if answered
                else None,
            }
        )
    return sorted(health, key=lambda row: -row["calls"])


def usage_report(session: Session, days: int, queued: int) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    tokens = func.coalesce(UsageEvent.prompt_tokens, 0) + func.coalesce(
        UsageEvent.completion_tokens, 0
    )
    by_day: list[dict[str, Any]] = [
        {
            "day": str(day),
            "tokens": int(total or 0),
            "cost_usd": round(float(cost or 0), 4),
            "calls": int(calls),
        }
        for day, total, cost, calls in session.execute(
            select(
                func.date(UsageEvent.occurred_at),
                func.sum(tokens),
                func.sum(UsageEvent.estimated_cost),
                func.count(UsageEvent.id),
            )
            .where(UsageEvent.occurred_at >= since)
            .group_by(func.date(UsageEvent.occurred_at))
            .order_by(func.date(UsageEvent.occurred_at))
        ).all()
    ]

    def group(column: Any) -> list[dict[str, Any]]:
        rows = session.execute(
            select(
                column,
                func.count(UsageEvent.id),
                func.sum(tokens),
                func.sum(UsageEvent.estimated_cost),
                func.sum(case((func.lower(UsageEvent.status) != "ok", 1), else_=0)),
                func.avg(cast(UsageEvent.latency_seconds, Float)),
            )
            .where(UsageEvent.occurred_at >= since)
            .group_by(column)
            .order_by(func.sum(tokens).desc().nulls_last())
        ).all()
        return [
            {
                "key": str(key or "unknown"),
                "calls": int(calls),
                "tokens": int(total or 0),
                "cost_usd": round(float(cost or 0), 4),
                "failures": int(failures or 0),
                "avg_seconds": round(float(latency or 0), 2),
            }
            for key, calls, total, cost, failures, latency in rows
        ]

    latencies = [
        float(value)
        for (value,) in session.execute(
            select(UsageEvent.latency_seconds).where(UsageEvent.occurred_at >= since)
        ).all()
    ]
    statuses = {
        str(status): int(count)
        for status, count in session.execute(
            select(UsageEvent.status, func.count(UsageEvent.id))
            .where(UsageEvent.occurred_at >= since)
            .group_by(UsageEvent.status)
        ).all()
    }
    chat = session.execute(
        select(
            func.coalesce(func.sum(ChannelUsage.calls), 0),
            func.coalesce(func.sum(ChannelUsage.tokens), 0),
            func.sum(ChannelUsage.estimated_cost),
        ).where(ChannelUsage.day >= since.date())
    ).one()
    total_tokens = sum(int(row["tokens"]) for row in by_day) + int(chat[1])
    total_cost = sum(float(row["cost_usd"]) for row in by_day) + float(chat[2] or 0)
    priced = session.scalar(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.occurred_at >= since, UsageEvent.estimated_cost.is_not(None)
        )
    )
    calls = sum(int(row["calls"]) for row in by_day)
    return {
        "since": since.date().isoformat(),
        "days": days,
        "totals": {
            "tokens": total_tokens,
            "cost_usd": round(total_cost, 4),
            "calls": calls + int(chat[0]),
            # Some providers report no price, so cost is a lower bound then.
            "cost_complete": int(priced or 0) == calls,
        },
        "by_day": by_day,
        "by_role": group(UsageEvent.role),
        "by_model": group(func.coalesce(UsageEvent.resolved_model, UsageEvent.model_alias)),
        "chat": {
            "calls": int(chat[0]),
            "tokens": int(chat[1]),
            "cost_usd": round(float(chat[2] or 0), 4),
        },
        "performance": {
            "calls": calls,
            "failures": sum(count for status, count in statuses.items() if status.lower() != "ok"),
            "avg_seconds": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "p95_seconds": _percentile(latencies, 0.95),
            "slowest_seconds": round(max(latencies), 2) if latencies else 0.0,
            "queued_now": queued,
            "statuses": statuses,
        },
        "models_configured": int(session.scalar(select(func.count(ModelConnection.id))) or 0),
        "model_health": model_health(session, since),
    }
