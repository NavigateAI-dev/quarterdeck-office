"""The day's digest, as one card, to the channels that asked for it.

From 18:00 in the owner's zone, once a day per channel. Counts only: what
finished, what failed, what waits for the owner and what the day cost. Never
what the work says, because a notification channel may be a shared room. A
day with nothing to count says nothing.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import ideas
from app.channels import slack_format
from app.channels.results import office_origin
from app.db.models import (
    ChannelApp,
    ChannelDelivery,
    ChannelDestination,
    ChannelInstallation,
    Run,
    UsageEvent,
)

EVENT = "daily_digest"
HOUR = 18
WAITING = ("WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "PAUSED")


def counts(session: Session, since: datetime) -> dict[str, Any]:
    """What the office did since `since`, and what waits for the owner now."""

    def finished(status: str) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.status == status, Run.completed_at >= since)
            )
            or 0
        )

    waiting = session.scalar(select(func.count()).select_from(Run).where(Run.status.in_(WAITING)))
    cost = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0)).where(
            UsageEvent.occurred_at >= since
        )
    )
    return {
        "finished": finished("COMPLETED"),
        "failed": finished("FAILED"),
        "waiting": int(waiting or 0),
        "cost": float(cost or 0.0),
    }


def digest_card(day: str, found: dict[str, Any], link: str | None) -> slack_format.Card:
    waiting = found["waiting"]
    return slack_format.Card(
        kind="Daily digest",
        title=f"The office's day, {day}",
        tone="waiting" if waiting else "info",
        status=f"{waiting} waiting for you" if waiting else "Nothing waiting",
        facts=(
            ("Finished", str(found["finished"])),
            ("Failed", str(found["failed"])),
            ("Waiting for you", str(waiting)),
            ("Spent today", f"${found['cost']:.2f}"),
        ),
        footer="_Counts only: what the work says stays in the office._",
        buttons=tuple(slack_format.open_button(link)),
    )


def notify_daily_digest(session_factory: sessionmaker[Session], now: datetime) -> int:
    """Queue today's digest for each channel that asked, once a day. Returns how many."""
    with session_factory() as session, session.begin():
        zone = ideas.owner_zone(session)
        local = now.astimezone(zone)
        if local.hour < HOUR:
            return 0
        since = datetime.combine(local.date(), time(0), tzinfo=zone)
        found = counts(session, since)
        if not any((found["finished"], found["failed"], found["waiting"], found["cost"])):
            return 0
        origin = office_origin(session)
        card = digest_card(f"{local:%a %d %b}", found, origin)
        text, blocks = slack_format.card_text(card), slack_format.card_blocks(card)
        destinations = session.execute(
            select(ChannelDestination, ChannelApp.id, ChannelApp.agent_profile_id)
            .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
            .join(ChannelApp, ChannelApp.id == ChannelInstallation.app_id)
            .where(ChannelDestination.enabled.is_(True), ChannelInstallation.status == "ACTIVE")
        ).all()
        created = 0
        for destination, app_id, agent_profile_id in destinations:
            # The office bot's digest: an agent's bot speaks only of its own work.
            if EVENT not in (destination.events_json or []) or agent_profile_id is not None:
                continue
            result = session.execute(
                insert(ChannelDelivery)
                .values(
                    destination_id=destination.id,
                    run_id=None,
                    event_kind=EVENT,
                    idempotency_key=f"digest:{local.date().isoformat()}/{destination.id}",
                    text=text,
                    status="QUEUED",
                    app_id=app_id,
                    blocks_json=blocks,
                )
                .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
                .returning(ChannelDelivery.id)
            )
            created += 1 if result.scalar_one_or_none() is not None else 0
    return created
