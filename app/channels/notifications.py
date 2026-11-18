"""Owner notifications through chat channels.

Run outcomes become delivery rows keyed by run, event and destination, so a
replayed notification can never create a second message. A send whose outcome
is unknown is left for the owner to reconcile instead of being retried.

Notification channels, often shared rooms, get the status, where to act and a
link, never the work's content. The thread the work was asked in is the owner's
own conversation, so it gets the work itself: the answer, the document as
a file, and buttons to act on it (`app.channels.results`).
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import ideas, widgets
from app.artifacts import ArtifactStore
from app.calendars.store import holding
from app.channels import results, slack_format
from app.channels.base import ChannelError, OutboundMessage
from app.channels.briefs import queue_briefs
from app.channels.progress import origin_destination, sync_progress
from app.channels.registry import adapter_for
from app.config import Settings
from app.credentials import resolve_credential
from app.db.models import (
    ChannelApp,
    ChannelDelivery,
    ChannelDestination,
    ChannelInstallation,
    LimitAlert,
    Project,
    Run,
    RunEvent,
    Task,
)
from app.secret_store import SecretStoreError

logger = logging.getLogger("quarterdeck.notifications")
EVENT_OF_STATUS = {
    "WAITING_FOR_APPROVAL": "needs_approval",
    "NEEDS_HUMAN_REVIEW": "needs_input",
    "COMPLETED": "completed",
    "FAILED": "failed",
}
PHRASE = {
    "needs_approval": "needs your approval",
    "needs_input": "stopped and needs your input",
    "completed": "has finished",
    "failed": "failed",
    # Not a task's outcome: a limit reaching 80% or 100%.
    "limit_warning": "reached a limit",
    # Not a task's outcome: ideas not planned yet, on Friday afternoon.
    "ideas_friday": "has ideas to plan",
    # Not a task's outcome: the day's counts, from 18:00 (`app.channels.digest`).
    "daily_digest": "has its daily digest",
}
LIMIT_EVENT = "limit_warning"
IDEAS_EVENT = "ideas_friday"
# A task's live progress message, kept in the same ledger.
PROGRESS = "progress"
EVENTS = frozenset(PHRASE)
MAX_ATTEMPTS = 3
TICK_SECONDS = 10


def notify_run(
    session_factory: sessionmaker[Session],
    run_id: object,
    status: str,
    store: ArtifactStore | None = None,
    page: Callable[[object], str | None] | None = None,
) -> int:
    """Queue one delivery per subscribed destination. Returns how many were new.

    With the artifact store, the thread the work was asked in gets the work
    itself; without it, the one line every destination gets.
    """
    event = EVENT_OF_STATUS.get(status)
    if event is None:
        return 0
    with session_factory() as session, session.begin():
        run = session.get(Run, run_id)
        if run is None:
            return 0
        task = session.get(Task, run.root_task_id)
        project = session.get(Project, run.project_id)
        objective = (task.objective if task else "a task")[:80]
        where = f" in {project.name}" if project else ""
        link = results.office_link(session, run.id)
        text = f"Quarterdeck: “{objective}”{where} {PHRASE[event]}. " + (
            f"Open it: {link}" if link else "Open your office on your computer to see it."
        )
        # A reworked task finishes again, and says so again.
        cycle = session.scalar(
            select(func.count())
            .select_from(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.kind == "rework_requested")
        )
        again = f"/r{cycle}" if cycle else ""
        chosen = project.notify_destination_id if project else None
        listing = (
            select(ChannelDestination, ChannelApp.id, ChannelApp.agent_profile_id)
            .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
            .join(ChannelApp, ChannelApp.id == ChannelInstallation.app_id)
            .where(ChannelDestination.enabled.is_(True), ChannelInstallation.status == "ACTIVE")
        )
        if chosen is not None:
            # A project with its own channel is not announced anywhere else.
            listing = listing.where(ChannelDestination.id == chosen)
        destinations = session.execute(listing).all()
        origin = (run.config_snapshot_json or {}).get("origin") or {}
        lead = origin.get("agent_profile_id")
        Planned = tuple[
            UUID, str, UUID, UUID | None, str | None, str, list[Any] | None, list[Any] | None
        ]
        planned: list[Planned] = []
        reply = origin_destination(session, origin)
        for destination, app_id, agent_profile_id in destinations:
            if event not in (destination.events_json or []):
                continue
            if reply is not None and destination.id == reply[0]:
                continue  # The channel a report is posted to gets the report, not a line too.
            # An agent's bot posts only about work that agent was asked to do;
            # the office bot posts about everything.
            if agent_profile_id is not None and str(agent_profile_id) != lead:
                continue
            planned.append(
                (
                    destination.id,
                    str(destination.id),
                    app_id,
                    agent_profile_id,
                    None,
                    text,
                    None,
                    None,
                )
            )
        if reply is not None:
            destination_id, app_id, agent_profile_id = reply
            message = (
                results.thread_message(session, store, run, event, page)
                if store is not None
                else None
            )
            planned.append(
                (
                    destination_id,
                    "origin",
                    app_id,
                    agent_profile_id,
                    origin.get("thread"),
                    message.text
                    if message
                    else f"{PHRASE[event].capitalize()}: “{objective}”{where}. "
                    "Ask me here about the result, or open your office to act on it.",
                    message.blocks if message else None,
                    message.files if message and message.files else None,
                )
            )
        created = 0
        for destination_id, key, app_id, agent_profile_id, thread, body, blocks, files in planned:
            result = session.execute(
                insert(ChannelDelivery)
                .values(
                    destination_id=destination_id,
                    run_id=run.id,
                    event_kind=event,
                    idempotency_key=f"run:{run.id}/{event}/{key}{again}",
                    text=body,
                    status="QUEUED",
                    app_id=app_id,
                    agent_profile_id=agent_profile_id,
                    thread_ref=thread,
                    blocks_json=blocks,
                    files_json=files,
                )
                .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
                .returning(ChannelDelivery.id)
            )
            created += 1 if result.scalar_one_or_none() is not None else 0
        return created


def _limit_said(alert: LimitAlert) -> tuple[str, str]:
    """Which limit and how far, and what happens now it is reached."""
    share = round(float(alert.share) * 100)
    at_limit = alert.threshold >= 100
    after = ""
    if alert.scope == "office":
        said = (
            "The office has used all of this month's spending limit"
            if at_limit
            else f"The office has used {share}% of this month's spending limit"
        )
        if at_limit:
            after = "New work waits until the month turns or you raise it."
    elif alert.scope == "project":
        said = (
            f"“{alert.name}” has used all of its monthly limit"
            if at_limit
            else f"“{alert.name}” has used {share}% of its monthly limit"
        )
    elif alert.scope == "task":
        said = (
            f"“{alert.name}” has used all of its budget"
            if at_limit
            else f"“{alert.name}” has used {share}% of its budget"
        )
        if at_limit:
            after = "It waits for you to raise it."
    else:
        said = (
            f"The {alert.name} account has run out"
            if at_limit
            else f"The {alert.name} account is running low"
        )
    return said, after


def limit_text(alert: LimitAlert) -> str:
    """What a limit warning says: which limit, how far, where to act. Nothing else."""
    said, after = _limit_said(alert)
    return f"Quarterdeck: {said}.{' ' + after if after else ''} Open your office to see it."


LIMIT_OF = {
    "office": "The office's month",
    "project": "A project's month",
    "task": "A task's budget",
}


def limit_card(alert: LimitAlert, link: str | None) -> slack_format.Card:
    """The warning as a system alert card: the same words, with its facts."""
    said, after = _limit_said(alert)
    at_limit = alert.threshold >= 100
    return slack_format.Card(
        kind="System alert",
        title=f"{said}.",
        tone="alert" if at_limit else "waiting",
        status="Limit reached" if at_limit else "Limit nearly reached",
        body=slack_format.escaped(after),
        facts=(
            ("Severity", "High" if at_limit else "Medium"),
            ("Limit", LIMIT_OF.get(alert.scope, "A provider account")),
            ("Used", f"{round(float(alert.share) * 100)}%"),
        ),
        buttons=tuple(slack_format.open_button(link)),
    )


def _project_of(session: Session, alert: LimitAlert) -> Project | None:
    try:
        if alert.scope == "project":
            return session.get(Project, UUID(alert.subject))
        if alert.scope == "task":
            run = session.get(Run, UUID(alert.subject))
            return session.get(Project, run.project_id) if run is not None else None
    except ValueError:
        return None
    return None


def notify_limits(session_factory: sessionmaker[Session]) -> int:
    """Queue each new limit warning for the channels that asked for them, once.

    The office bot says it; an agent's bot speaks only of its own work. A
    project with its own channel hears its project's and tasks' warnings
    there, and nowhere else, as with its tasks' outcomes.
    """
    created = 0
    with session_factory() as session, session.begin():
        fresh = list(
            session.scalars(
                select(LimitAlert)
                .where(LimitAlert.notified_at.is_(None))
                .order_by(LimitAlert.created_at)
                .limit(50)
            )
        )
        if not fresh:
            return 0
        destinations = session.execute(
            select(ChannelDestination, ChannelApp.id, ChannelApp.agent_profile_id)
            .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
            .join(ChannelApp, ChannelApp.id == ChannelInstallation.app_id)
            .where(ChannelDestination.enabled.is_(True), ChannelInstallation.status == "ACTIVE")
        ).all()
        for alert in fresh:
            project = _project_of(session, alert)
            chosen = project.notify_destination_id if project is not None else None
            text = limit_text(alert)
            card = limit_card(alert, results.office_origin(session))
            blocks = slack_format.card_blocks(card)
            for destination, app_id, agent_profile_id in destinations:
                if LIMIT_EVENT not in (destination.events_json or []):
                    continue
                if agent_profile_id is not None:
                    continue
                if chosen is not None and destination.id != chosen:
                    continue
                result = session.execute(
                    insert(ChannelDelivery)
                    .values(
                        destination_id=destination.id,
                        run_id=UUID(alert.subject) if alert.scope == "task" else None,
                        event_kind=LIMIT_EVENT,
                        idempotency_key=f"limit:{alert.id}/{destination.id}",
                        text=text,
                        status="QUEUED",
                        app_id=app_id,
                        blocks_json=blocks,
                    )
                    .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
                    .returning(ChannelDelivery.id)
                )
                created += 1 if result.scalar_one_or_none() is not None else 0
            alert.notified_at = datetime.now(UTC)
    return created


def notify_ideas_friday(session_factory: sessionmaker[Session], now: datetime) -> int:
    """Friday from 16:00, in the owner's zone: tell the channels that asked how
    many ideas wait to be planned. Once a Friday per channel, by its key;
    the count only, never what the ideas say (a channel may be shared)."""

    with session_factory() as session, session.begin():
        local = now.astimezone(ideas.owner_zone(session))
        if local.weekday() != 4 or local.hour < ideas.FRIDAY_HOUR:
            return 0
        board = ideas.board(session, now)
        waiting = int(board["counts"]["new"])
        late = sum(1 for i in board["today"] if i["bucket"] == "overdue")
        if not waiting and not late:
            return 0
        parts = [f"{waiting} idea{'' if waiting == 1 else 's'} not planned yet"] if waiting else []
        if late:
            parts.append(f"{late} from a day that has passed")
        text = (
            f"Friday: {' and '.join(parts)}. Give them a day under Ideas on the floor "
            "before the weekend."
        )
        destinations = session.execute(
            select(ChannelDestination, ChannelApp.id, ChannelApp.agent_profile_id)
            .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
            .join(ChannelApp, ChannelApp.id == ChannelInstallation.app_id)
            .where(ChannelDestination.enabled.is_(True), ChannelInstallation.status == "ACTIVE")
        ).all()
        created = 0
        for destination, app_id, agent_profile_id in destinations:
            if IDEAS_EVENT not in (destination.events_json or []) or agent_profile_id is not None:
                continue
            result = session.execute(
                insert(ChannelDelivery)
                .values(
                    destination_id=destination.id,
                    run_id=None,
                    event_kind=IDEAS_EVENT,
                    idempotency_key=f"ideas:{local.date().isoformat()}/{destination.id}",
                    text=text,
                    status="QUEUED",
                    app_id=app_id,
                )
                .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
                .returning(ChannelDelivery.id)
            )
            created += 1 if result.scalar_one_or_none() is not None else 0
    return created


async def reconcile_unknown(
    session_factory: sessionmaker[Session], settings: Settings, window_seconds: int = 3600
) -> dict[str, int]:
    """Resolve deliveries whose outcome the platform never confirmed.

    The send may or may not have arrived, so it is never repeated blindly.
    Where the platform can be read, its own record decides; where it cannot,
    the delivery waits for the owner rather than risking a second message.
    """
    with session_factory() as session:
        rows = session.execute(
            select(ChannelDelivery.id, ChannelDestination.external_id, ChannelInstallation)
            .join(ChannelDestination, ChannelDestination.id == ChannelDelivery.destination_id)
            .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
            .where(
                ChannelDelivery.status == "UNKNOWN",
                ChannelDelivery.reconciled_at.is_(None),
            )
            .limit(50)
        ).all()
    settled = {"delivered": 0, "not_delivered": 0, "still_unknown": 0}
    for delivery_id, channel, installation in rows:
        with session_factory() as session:
            delivery = session.get(ChannelDelivery, delivery_id)
            text = delivery.text if delivery else ""
            since = (delivery.created_at.timestamp() - 60) if delivery else 0.0
        adapter = adapter_for(installation.kind)
        finder = getattr(adapter, "find_message", None)
        if finder is None:
            settled["still_unknown"] += 1
            continue
        try:
            token = resolve_credential(installation.token_ref, settings)
            reference = await finder(token, channel, text, max(since, 0.0))
        except (ChannelError, SecretStoreError):
            settled["still_unknown"] += 1
            continue
        with session_factory() as session, session.begin():
            row = session.get(ChannelDelivery, delivery_id)
            if row is None:
                continue
            row.reconciled_at = datetime.now(UTC)
            row.reconciled_by = "platform"
            if reference:
                row.status = "SENT"
                row.external_ref = reference
                row.error = None
                settled["delivered"] += 1
            else:
                # The platform is sure it never arrived, so sending again
                # cannot duplicate it.
                row.status = "QUEUED"
                row.error = None
                settled["not_delivered"] += 1
    return settled


def resolve_by_owner(
    session_factory: sessionmaker[Session], delivery_id: object, arrived: bool
) -> dict[str, Any]:
    """The owner says whether an unknown delivery arrived."""
    with session_factory() as session, session.begin():
        row = session.get(ChannelDelivery, delivery_id)
        if row is None or row.status != "UNKNOWN":
            raise ChannelError("not_unknown")
        row.reconciled_at = datetime.now(UTC)
        row.reconciled_by = "owner"
        row.status = "SENT" if arrived else "QUEUED"
        row.error = None if arrived else row.error
        return {"id": str(row.id), "status": row.status}


async def deliver_pending(session_factory: sessionmaker[Session], settings: Settings) -> int:
    """Send queued deliveries and retry plain failures; never retry UNKNOWN.

    Nothing goes while the owner is in a meeting and asked for the in-tray to
    be held then: the deliveries stay queued and go when it ends.
    """
    with session_factory() as session:
        # Only while the calendar is on the owner's wall.
        if widgets.placed(session, "calendar") and holding(session, datetime.now(UTC)):
            return 0
        rows = session.execute(
            select(
                ChannelDelivery.id,
                ChannelDestination.external_id,
                ChannelInstallation,
                ChannelDelivery.thread_ref,
            )
            .join(ChannelDestination, ChannelDestination.id == ChannelDelivery.destination_id)
            .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
            .where(
                # A progress message is posted and edited by its own loop.
                ChannelDelivery.event_kind != PROGRESS,
                or_(
                    ChannelDelivery.status == "QUEUED",
                    (ChannelDelivery.status == "FAILED")
                    & (ChannelDelivery.attempts < MAX_ATTEMPTS),
                ),
            )
            .order_by(ChannelDelivery.created_at)
            .limit(50)
        ).all()
    sent = 0
    for delivery_id, channel, installation, thread in rows:
        with session_factory() as session:
            delivery = session.get(ChannelDelivery, delivery_id)
            text = delivery.text if delivery else ""
            blocks = (delivery.blocks_json or None) if delivery else None
            files = list(delivery.files_json or []) if delivery else []
        status, reference, error, files_error = "SENT", None, None, None
        try:
            token = resolve_credential(installation.token_ref, settings)
            adapter = adapter_for(installation.kind)
            result = await adapter.send(
                token, channel, OutboundMessage(text=text, thread=thread, blocks=blocks)
            )
            reference = result.external_ref
            sent += 1
            if files:
                # Under the message: in its thread, or the thread it starts.
                under = thread or reference.partition(":")[2] or None
                files_error = await _upload(
                    session_factory, settings, adapter, token, channel, under, files
                )
        except SecretStoreError:
            status, error = "FAILED", "credential_unavailable"
        except ChannelError as failure:
            status, error = (
                ("UNKNOWN" if failure.code == "outcome_unknown" else "FAILED"),
                failure.code,
            )
        with session_factory() as session, session.begin():
            row = session.get(ChannelDelivery, delivery_id)
            if row is not None:
                row.status = status
                row.attempts += 1
                row.external_ref = reference
                row.error = error
                row.files_error = files_error
    return sent


async def _upload(
    session_factory: sessionmaker[Session],
    settings: Settings,
    adapter: Any,
    token: str,
    channel: str,
    thread: str | None,
    files: list[dict[str, str]],
) -> str | None:
    """Share a delivery's files after its message. Returns why one did not go.

    Sent once, after the message arrived: a file that fails is reported on the
    delivery, never a reason to send the message again.
    """
    store = ArtifactStore(settings.workspace_root)
    for wanted in files:
        with session_factory() as session:
            made = results.render_file(
                session, store, str(wanted.get("artifact_id")), str(wanted.get("format"))
            )
        if made is None:
            return "file_unavailable"
        name, data = made
        try:
            await adapter.upload(token, channel, thread, name, data)
        except ChannelError as failure:
            # Most often missing_scope: the Slack app was made before files:write.
            logger.warning('{"event":"file_not_shared","code":"%s"}', failure.code)
            return failure.code[:64]
    return None


async def run_delivery_loop(
    session_factory: sessionmaker[Session], settings: Settings, stop: asyncio.Event
) -> None:

    while not stop.is_set():
        try:
            # A task's research brief, queued here and sent with the rest.
            await asyncio.to_thread(queue_briefs, session_factory)
        except Exception:
            logger.exception('{"event":"brief_tick_failed"}')
        try:
            await deliver_pending(session_factory, settings)
        except Exception:
            logger.exception('{"event":"delivery_tick_failed"}')
        try:
            await sync_progress(session_factory, settings)
        except Exception:
            logger.exception('{"event":"progress_tick_failed"}')
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except TimeoutError:
            continue
