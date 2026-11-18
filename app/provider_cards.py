"""One card per provider, for Settings → Models.

Each provider the office can use: who uses it, what it
has left where the provider says, when it was last called and how often it
failed in the last hour, the backup the owner chose, and whether it is set
aside now. Read from what the office already records; nothing is called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_profiles import capture_agent_snapshot
from app.config import Settings
from app.db.models import LimitAlert, ModelConnection, RunEvent, UsageEvent
from app.failover import BREAKER, GATEWAY, Breaker
from app.office_settings import effective, provider_backups, spent_this_month

ROLES = ("chief", "researcher", "strategist", "critic", "advisor")
ROLE_NAME = {
    "chief": "Chief",
    "researcher": "Researcher",
    "strategist": "Strategist",
    "critic": "Critic",
    "advisor": "Advisor",
}
# Calls made through the LiteLLM gateway before it was retired, in history.
RETIRED = "Office gateway (retired)"
# A connection a role may use: checked and answering.
USABLE = ("MODEL_AVAILABLE", "VERIFIED")


def key_of(model_alias: str | None) -> str:
    alias = str(model_alias or "")
    return alias.removeprefix("connection:") if alias.startswith("connection:") else GATEWAY


def cards(
    session: Session,
    settings: Settings,
    balances: list[dict[str, Any]],
    breaker: Breaker = BREAKER,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    office = effective(session, settings)
    chosen_roles: dict[str, str] = dict(office.get("role_models") or {})
    backups = provider_backups(session)
    connections = list(
        session.scalars(select(ModelConnection).order_by(ModelConnection.display_name))
    )
    agents = capture_agent_snapshot(session).get("agents", [])

    # Since the gateway left, a role nobody gave a model has none.
    used_by: dict[str, list[str]] = {}
    without_model: list[str] = []
    for role in ROLES:
        by_agent = [a for a in agents if a.get("role") == role]
        if by_agent:
            for agent in by_agent:
                where = str(agent.get("connection_id") or "") or chosen_roles.get(role)
                if where:
                    used_by.setdefault(where, []).append(f"{agent['name']} ({ROLE_NAME[role]})")
                else:
                    without_model.append(f"{agent['name']} ({ROLE_NAME[role]})")
        elif chosen_roles.get(role):
            used_by.setdefault(chosen_roles[role], []).append(ROLE_NAME[role])
        elif role != "advisor":
            without_model.append(ROLE_NAME[role])
    for agent in agents:
        if agent.get("role") not in ROLES and agent.get("connection_id"):
            used_by.setdefault(str(agent["connection_id"]), []).append(str(agent["name"]))

    hour_ago = now - timedelta(hours=1)
    recent: dict[str, dict[str, Any]] = {}
    for alias, calls, errors in session.execute(
        select(
            UsageEvent.model_alias,
            func.count(),
            func.count().filter(UsageEvent.status != "ok"),
        )
        .where(UsageEvent.occurred_at >= hour_ago, UsageEvent.role != "tools")
        .group_by(UsageEvent.model_alias)
    ):
        entry = recent.setdefault(key_of(alias), {"calls": 0, "errors": 0})
        entry["calls"] += int(calls)
        entry["errors"] += int(errors)
    last_call: dict[str, datetime] = {}
    for alias, at in session.execute(
        select(UsageEvent.model_alias, func.max(UsageEvent.occurred_at))
        .where(UsageEvent.role != "tools")
        .group_by(UsageEvent.model_alias)
    ):
        key = key_of(alias)
        if at is not None and (key not in last_call or at > last_call[key]):
            last_call[key] = at
    moved: dict[str, int] = {}
    for (payload,) in session.execute(
        select(RunEvent.payload_json).where(
            RunEvent.kind == "model_failed_over", RunEvent.created_at >= now - timedelta(days=7)
        )
    ):
        key = str((payload or {}).get("from_key") or "")
        moved[key] = moved.get(key, 0) + 1
    by_balance = {str(entry.get("connection_id")): entry for entry in balances}
    names = {GATEWAY: RETIRED} | {str(row.id): row.display_name for row in connections}

    def card(key: str, **base: Any) -> dict[str, Any]:
        plan = backups.get(key) or {}
        backup = str(plan.get("backup") or "") or None
        aside = breaker.view(key)
        return {
            "key": key,
            **base,
            "used_by": used_by.get(key, []),
            "last_call_at": last_call[key].isoformat() if key in last_call else None,
            "last_hour": recent.get(key, {"calls": 0, "errors": 0}),
            "backup": {
                "key": backup,
                "name": names.get(backup) if backup else None,
                "come_back": bool(plan.get("come_back", True)),
            },
            "failed_over": {
                "reason": aside["reason"],
                "since": datetime.fromtimestamp(aside["at"], UTC).isoformat()
                if aside.get("at")
                else None,
            }
            if aside
            else None,
            "failovers_week": moved.get(key, 0),
        }

    found: list[dict[str, Any]] = []
    for row in connections:
        key = str(row.id)
        reading = by_balance.get(key) or {}
        found.append(
            card(
                key,
                name=row.display_name,
                provider=row.provider,
                model=row.model_id,
                status=row.status,
                usable=row.status in USABLE,
                balance={
                    "state": reading.get("state"),
                    "usable": reading.get("usable"),
                    "total": (reading.get("balance") or {}).get("total"),
                    "currency": (reading.get("balance") or {}).get("currency"),
                    "remaining": (reading.get("limit") or {}).get("remaining"),
                    "limit": (reading.get("limit") or {}).get("limit"),
                    "reason": reading.get("reason"),
                    "checked_at": reading.get("checked_at"),
                }
                if reading
                else None,
            )
        )
    for entry in found:
        entry["needs_you"] = _needs_you(entry)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    calls_today = session.scalar(
        select(func.count()).where(UsageEvent.occurred_at >= today, UsageEvent.role != "tools")
    )
    return {
        "providers": found,
        "roles": {role: chosen_roles.get(role) for role in ROLES},
        "without_model": without_model,
        "summary": {
            "spent_this_month_usd": round(spent_this_month(session), 4),
            "monthly_limit_usd": office.get("monthly_cost_limit_usd"),
            "calls_today": int(calls_today or 0),
            "failovers_week": sum(moved.values()),
            "needs_you": sum(1 for entry in found if entry["needs_you"])
            + (1 if without_model else 0),
        },
    }


def _needs_you(entry: dict[str, Any]) -> str | None:
    """What the owner should do about this provider, if anything."""
    balance = entry.get("balance") or {}
    if balance.get("state") == "ok" and balance.get("usable") is False:
        return "out_of_credit"
    if entry.get("failed_over"):
        return "failed_over"
    if not entry.get("usable"):
        return "not_ready"
    return None


def valid_key(session: Session, key: str) -> bool:
    """A connection that exists; the retired gateway is no longer one."""
    try:
        return session.get(ModelConnection, UUID(key)) is not None
    except ValueError:
        return False


REASON_WORDS = {
    "no_credit": "ran out of credit",
    "key_refused": "refused its key",
    "failing": "kept failing",
}


def usage(session: Session, days: int, now: datetime | None = None) -> dict[str, Any]:
    """Calls, tokens, cost, errors and failovers per provider over `days`.

    Cache hits are not recorded for model calls, so they are not shown.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    names = {GATEWAY: RETIRED} | {
        str(row.id): row.display_name for row in session.scalars(select(ModelConnection))
    }
    models = {str(row.id): row.model_id for row in session.scalars(select(ModelConnection))}
    tokens = func.coalesce(UsageEvent.prompt_tokens, 0) + func.coalesce(
        UsageEvent.completion_tokens, 0
    )
    rows: dict[str, dict[str, Any]] = {}

    def row_for(key: str) -> dict[str, Any]:
        return rows.setdefault(
            key,
            {
                "key": key,
                "name": names.get(key, "A removed connection"),
                "model": models.get(key, "the role aliases" if key == GATEWAY else ""),
                "calls": 0,
                "tokens": 0,
                "cost_usd": 0.0,
                "errors": 0,
                "failovers_out": 0,
                "failovers_in": 0,
            },
        )

    for alias, calls, used, cost, errors in session.execute(
        select(
            UsageEvent.model_alias,
            func.count(),
            func.sum(tokens),
            func.sum(func.coalesce(UsageEvent.estimated_cost, 0)),
            func.count().filter(UsageEvent.status != "ok"),
        )
        .where(UsageEvent.occurred_at >= since, UsageEvent.role != "tools")
        .group_by(UsageEvent.model_alias)
    ):
        entry = row_for(key_of(alias))
        entry["calls"] += int(calls)
        entry["tokens"] += int(used or 0)
        entry["cost_usd"] = round(entry["cost_usd"] + float(cost or 0), 4)
        entry["errors"] += int(errors)

    day = func.date_trunc("day", UsageEvent.occurred_at)
    by_day: dict[str, dict[str, float]] = {}
    for when_, alias, cost in session.execute(
        select(day, UsageEvent.model_alias, func.sum(func.coalesce(UsageEvent.estimated_cost, 0)))
        .where(UsageEvent.occurred_at >= since, UsageEvent.role != "tools")
        .group_by(day, UsageEvent.model_alias)
    ):
        bucket = by_day.setdefault(when_.date().isoformat(), {})
        key = key_of(alias)
        bucket[key] = round(bucket.get(key, 0.0) + float(cost or 0), 4)

    happened: list[dict[str, Any]] = []
    for at, payload in session.execute(
        select(RunEvent.created_at, RunEvent.payload_json)
        .where(RunEvent.kind == "model_failed_over", RunEvent.created_at >= since)
        .order_by(RunEvent.created_at.desc())
    ):
        data = payload or {}
        row_for(str(data.get("from_key") or ""))["failovers_out"] += 1
        row_for(str(data.get("to_key") or ""))["failovers_in"] += 1
        reason = REASON_WORDS.get(str(data.get("reason")), "kept failing")
        happened.append(
            {
                "at": at.isoformat() if at else None,
                "text": f"{data.get('from')} {reason}; "
                f"{data.get('role')} moved to {data.get('to')}",
            }
        )
    for alert in session.scalars(
        select(LimitAlert)
        .where(LimitAlert.scope == "provider", LimitAlert.created_at >= since)
        .order_by(LimitAlert.created_at.desc())
    ):
        happened.append(
            {
                "at": alert.created_at.isoformat() if alert.created_at else None,
                "text": f"{alert.name or 'A provider'} "
                + ("ran out of balance" if alert.threshold >= 100 else "ran low on balance"),
            }
        )
    happened.sort(key=lambda line: line["at"] or "", reverse=True)
    rows.pop("", None)
    return {
        "days_asked": days,
        "providers": sorted(rows.values(), key=lambda r: -r["cost_usd"]),
        "days": [{"day": key, "by_provider": by_day[key]} for key in sorted(by_day)],
        "names": names,
        "happened": happened[:20],
    }
