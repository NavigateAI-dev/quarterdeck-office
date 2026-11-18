"""Dispatch due schedules. Policy lives here; time math in scheduling.py.

- An occurrence is skipped while the previous scheduled run is still active
  (running, paused, or waiting for the owner): runs never pile up.
- A schedule whose project was deleted, or whose last run exhausted its
  budget, is disabled with the reason.
- Missed occurrences are skipped: the next run is the next future one.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import desk, provider_balance, topic_briefs, watches, widgets
from app.channels.digest import notify_daily_digest
from app.channels.notifications import notify_ideas_friday, notify_limits
from app.config import Settings
from app.db.models import (
    ChannelApp,
    ChannelDestination,
    ChannelInstallation,
    Run,
    Schedule,
    TaskTemplate,
)
from app.judgments import build_judge
from app.mcp.store import allow_hosts
from app.office_pulse import record_crossings
from app.office_settings import MonthlyLimitReached, OfficePaused, paused
from app.orchestrator.runner import RunRepository, UnknownProject
from app.room_replies import RoomReplies
from app.routines.run import routine_pass
from app.run_start import start_run
from app.runtime.worker import RunWorker
from app.scheduling import Recurrence, next_occurrence
from app.skills import updates as skill_updates
from app.venture_routing import auto_sort

logger = logging.getLogger("quarterdeck.scheduler")
ACTIVE = frozenset(
    {"PENDING", "RUNNING", "RECOVERING", "PAUSED", "WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW"}
)
TICK_SECONDS = 30
# How often projects waiting for a business line are sorted.
SORT_SECONDS = 120
# Discussions gone quiet are briefed this often, a few at a time.
BRIEF_SECONDS = 120
# How often provider balances are read without a browser asking.
BALANCE_SECONDS = 900
# Skills from GitHub are checked for updates daily, the first time ten
# minutes after the office starts rather than at every restart.
SKILL_CHECK_SECONDS = 86_400
SKILL_CHECK_FIRST_SECONDS = 600


def rule_of(schedule: Schedule) -> Recurrence:
    return Recurrence(
        timezone=schedule.timezone,
        frequency=schedule.frequency,
        local_time=schedule.local_time,
        weekday=schedule.weekday,
    )


DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def cadence(schedule: Schedule) -> str:
    """When it runs, as the owner set it: "every Monday at 09:00"."""
    day = (
        f"every {DAYS[schedule.weekday]}"
        if schedule.frequency == "weekly" and schedule.weekday is not None
        else "every day"
    )
    return f"{day} at {schedule.local_time}"


def standing_origin(session: Session, schedule: Schedule) -> dict[str, Any] | None:
    """Where a standing report is posted: the channel the owner chose.

    Posted as the work asked in a thread is, answer, file and buttons,
    but as a message of its own in the channel, with no progress message: a
    channel is a shared room, and a report is read when it arrives.
    """
    if schedule.post_to_id is None:
        return None
    destination = session.get(ChannelDestination, schedule.post_to_id)
    installation = (
        session.get(ChannelInstallation, destination.installation_id) if destination else None
    )
    if destination is None or installation is None or installation.status != "ACTIVE":
        return None
    app = session.get(ChannelApp, installation.app_id)
    return {
        "kind": installation.kind,
        "app_id": str(installation.app_id),
        "agent_profile_id": str(app.agent_profile_id) if app and app.agent_profile_id else None,
        "channel": destination.external_id,
        "thread": None,
        "standing": cadence(schedule),
    }


def tick(
    session_factory: sessionmaker[Session],
    repository: RunRepository,
    worker: RunWorker,
    settings: Settings,
    now: datetime,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    with session_factory() as session:
        due = list(
            session.scalars(
                select(Schedule).where(
                    Schedule.prompt.is_(None),
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at <= now,
                )
            )
        )
    for schedule in due:
        outcome = _handle(session_factory, repository, worker, settings, schedule, now)
        outcomes.append({"schedule_id": str(schedule.id)} | outcome)
    return outcomes


def _handle(
    session_factory: sessionmaker[Session],
    repository: RunRepository,
    worker: RunWorker,
    settings: Settings,
    schedule: Schedule,
    now: datetime,
) -> dict[str, Any]:
    with session_factory() as session:
        template = session.get(TaskTemplate, schedule.template_id)
        last = session.get(Run, schedule.last_run_id) if schedule.last_run_id else None
        last_status, last_error = (last.status, last.error or "") if last else (None, "")
    result: dict[str, Any]
    if template is None or template.project_id is None:
        result = {"outcome": "disabled", "reason": "project_deleted"}
    elif last_status in ACTIVE:
        result = {"outcome": "skipped_overlap", "run_id": str(schedule.last_run_id)}
    elif last_status == "FAILED" and "budget_exhausted" in last_error:
        result = {"outcome": "disabled", "reason": "budget_exhausted"}
    else:
        try:
            with session_factory() as session:
                if paused(session, settings, "schedules"):
                    # Not the schedule's fault: it runs again when resumed.
                    raise OfficePaused("schedules")
            with session_factory() as session:
                origin = standing_origin(session, schedule)
            run = start_run(
                session_factory,
                repository,
                worker,
                settings,
                template.project_id,
                template.objective,
                template.budget_json or None,
                origin=origin,
            )
            result = {"outcome": "started", "run_id": str(run.id)}
        except UnknownProject:
            result = {"outcome": "disabled", "reason": "project_deleted"}
        except OfficePaused:
            result = {"outcome": "skipped_paused"}
        except MonthlyLimitReached:
            # Not a fault of the schedule: it runs again once the month turns
            # or the owner raises the limit.
            result = {"outcome": "skipped_monthly_limit"}
    with session_factory() as session, session.begin():
        row = session.get(Schedule, schedule.id)
        if row is None:
            return result
        row.last_outcome = result["outcome"]
        if result["outcome"] == "disabled":
            row.enabled = False
            row.disabled_reason = result["reason"]
        else:
            if result["outcome"] == "started":
                row.last_run_id = result["run_id"]
            row.next_run_at = next_occurrence(rule_of(row), now)
    logger.info('{"event":"schedule_tick","outcome":"%s"}', result["outcome"])
    return result


def limit_pass(session_factory: sessionmaker[Session], settings: Settings) -> int:
    """Limits passed with the office closed are recorded and announced."""
    with session_factory() as session, session.begin():
        record_crossings(session, settings)
    return notify_limits(session_factory)


def _calendar_on(session_factory: sessionmaker[Session]) -> bool:
    with session_factory() as session:
        return widgets.placed(session, "calendar")


async def sort_pass(session_factory: sessionmaker[Session], judge: Any) -> dict[str, Any]:
    with session_factory() as session, session.begin():
        result = await auto_sort(session, judge)
    if result.get("assigned"):
        logger.info(
            '{"event":"projects_sorted_into_lines","assigned":%d,"unsure":%d}',
            len(result["assigned"]),
            len(result.get("unsure") or []),
        )
    return result


async def run_scheduler(
    session_factory: sessionmaker[Session],
    repository: RunRepository,
    worker: RunWorker,
    settings: Settings,
    stop: asyncio.Event,
    calendars: Any = None,
) -> None:
    """The background loop. Scheduled work runs only while the office is awake.

    Stops cooperatively: a tick in progress finishes before the loop exits, so
    shutdown never disposes the database under a running tick.
    """
    judge = build_judge(settings)
    sorted_at = 0.0
    briefed_at = 0.0

    replier = RoomReplies(session_factory, settings, repository.store)
    balances_at = 0.0
    skills_at = time.monotonic() - SKILL_CHECK_SECONDS + SKILL_CHECK_FIRST_SECONDS
    while not stop.is_set():
        try:
            await asyncio.to_thread(
                tick, session_factory, repository, worker, settings, datetime.now(UTC)
            )
        except Exception:
            logger.exception('{"event":"scheduler_tick_failed"}')
        try:
            # Discussions put off until today come back to the desk.
            await asyncio.to_thread(desk.returns_due, session_factory, datetime.now(UTC))
        except Exception:
            logger.exception('{"event":"returns_pass_failed"}')
        try:
            await asyncio.to_thread(limit_pass, session_factory, settings)
        except Exception:
            logger.exception('{"event":"limit_pass_failed"}')
        try:
            # Ideas not planned yet, to the channels that asked, on Friday.
            await asyncio.to_thread(notify_ideas_friday, session_factory, datetime.now(UTC))
        except Exception:
            logger.exception('{"event":"ideas_friday_failed"}')
        try:
            # The day's counts, from 18:00, to the channels that asked.
            await asyncio.to_thread(notify_daily_digest, session_factory, datetime.now(UTC))
        except Exception:
            logger.exception('{"event":"daily_digest_failed"}')
        if calendars is not None:
            try:
                # The owner's calendars, each read again once it is ten minutes old.
                # Paused while the calendar is off the wall; back on, it catches up.
                if _calendar_on(session_factory):
                    await calendars.sync_due()
            except Exception:
                logger.exception('{"event":"calendar_pass_failed"}')
        if time.monotonic() - balances_at >= BALANCE_SECONDS:
            balances_at = time.monotonic()
            try:
                # Reading them records a running-low or run-out warning.
                await provider_balance.balances(session_factory, settings, allow_hosts(settings))
            except Exception:
                logger.exception('{"event":"balance_pass_failed"}')
        if time.monotonic() - skills_at >= SKILL_CHECK_SECONDS:
            skills_at = time.monotonic()
            try:
                await skill_updates.check(session_factory, allow_hosts(settings))
            except Exception:
                logger.exception('{"event":"skill_check_failed"}')
        # Projects find their business line without the owner asking.
        if judge is not None and time.monotonic() - sorted_at >= SORT_SECONDS:
            sorted_at = time.monotonic()
            try:
                await sort_pass(session_factory, judge)
            except Exception:
                logger.exception('{"event":"line_sort_failed"}')
        try:
            # Routines whose time has come are answered in their discussions.
            await routine_pass(session_factory, settings, replier, datetime.now(UTC))
        except Exception:
            logger.exception('{"event":"routine_pass_failed"}')
        try:
            # Pages and topics the owner watches, whose time has come.
            await watches.watch_pass(session_factory, settings, replier.complete)
        except Exception:
            logger.exception('{"event":"watch_pass_failed"}')
        if time.monotonic() - briefed_at >= BRIEF_SECONDS:
            briefed_at = time.monotonic()
            try:
                await topic_briefs.brief_pass(session_factory, settings, replier.complete)
            except Exception:
                logger.exception('{"event":"brief_pass_failed"}')
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except TimeoutError:
            continue
