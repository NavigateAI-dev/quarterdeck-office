"""A task's progress in its Slack thread, as one message edited in place.

A task asked for in Slack used to be silent from "Started" until it finished,
which could be an hour. Now its thread holds one message that says which
steps are done and which is running, what a build is doing, and when the task
is stuck or waiting, with the buttons that fit at that moment.

It is one message, edited, never a new one per step: a thread that pinged on
every step would be muted by lunch. The message is a delivery row like any
other (`run:<id>/progress`), so a restart never posts it twice; its text is
what Slack shows now, so an edit is sent only when the text changes. A post
whose outcome is unknown is left for reconciliation, as every delivery is.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.channels import slack_format
from app.channels.base import ChannelError, OutboundMessage
from app.channels.registry import adapter_for
from app.channels.results import office_link, stop_text, task_about, task_facts
from app.config import Settings
from app.credentials import resolve_credential
from app.db.models import (
    ChannelApp,
    ChannelDelivery,
    ChannelDestination,
    ChannelInstallation,
    Run,
    RunEvent,
    Task,
)
from app.secret_store import SecretStoreError

logger = logging.getLogger("quarterdeck.progress")
EVENT = "progress"
ACTIVE = (
    "PENDING",
    "RUNNING",
    "RECOVERING",
    "PAUSED",
    "WAITING_FOR_APPROVAL",
    "NEEDS_HUMAN_REVIEW",
)
# A finished task's message gets its last edit within this long.
SETTLE = timedelta(minutes=30)
MAX_ATTEMPTS = 3
STEP = {
    "plan": "Planning",
    "replan": "Replanning",
    "research": "Research",
    "work": "Working through the plan",
    "strategy": "Analysis",
    "critique": "Review",
    "synthesis": "Writing the answer",
    "approval": "Approval",
    "engineering": "Building",
}


def origin_destination(
    session: Session, origin: dict[str, Any]
) -> tuple[UUID, UUID, UUID | None] | None:
    """The chat the work was requested from, as a (hidden) destination of its bot.

    Kept as a destination row so the delivery ledger records it like any other
    message; it is disabled, so it never receives notifications of other runs.
    """
    if not origin.get("app_id") or not origin.get("channel"):
        return None
    app = session.get(ChannelApp, UUID(origin["app_id"]))
    installation = session.scalar(
        select(ChannelInstallation).where(
            ChannelInstallation.app_id == UUID(origin["app_id"]),
            ChannelInstallation.status == "ACTIVE",
        )
    )
    if app is None or installation is None:
        return None  # The bot was disconnected since: nobody to post as.
    destination = session.scalar(
        select(ChannelDestination).where(
            ChannelDestination.installation_id == installation.id,
            ChannelDestination.external_id == origin["channel"],
        )
    )
    if destination is None:
        destination = ChannelDestination(
            installation_id=installation.id,
            external_id=str(origin["channel"])[:64],
            name="Chat thread",
            events_json=[],
            enabled=False,
        )
        session.add(destination)
        session.flush()
    return destination.id, app.id, app.agent_profile_id


def key_of(run_id: object) -> str:
    return f"run:{run_id}/{EVENT}"


def _steps(session: Session, run_id: UUID) -> tuple[list[str], set[str], str | None]:
    """Steps in the order they started, the finished ones, and the latest build phase."""
    order: list[str] = []
    done: set[str] = set()
    build: str | None = None
    rows = session.execute(
        select(RunEvent.kind, RunEvent.payload_json)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.kind.in_(("step_started", "step_completed", "build_step")),
        )
        .order_by(RunEvent.id)
    ).all()
    for kind, payload in rows:
        payload = payload or {}
        if kind == "build_step":
            # Its latest move, as the live build card shows it.
            if payload.get("kind") in ("phase", "now", "file", "command") and payload.get("text"):
                build = str(payload["text"])
            continue
        node = str(payload.get("node") or "")
        if not node:
            continue
        if node not in order:
            order.append(node)
        if kind == "step_completed":
            done.add(node)
        else:
            done.discard(node)
    return order, done, build


def _last(session: Session, run_id: UUID, kinds: tuple[str, ...]) -> RunEvent | None:
    return session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.kind.in_(kinds))
        .order_by(RunEvent.id.desc())
        .limit(1)
    ).first()


def stuck_on(session: Session, run_id: UUID) -> dict[str, Any] | None:
    """What a stuck step asks, until the task goes on or ends (as the office reads it)."""
    last = _last(session, run_id, ("stuck_ask", "run_resumed", "finish_requested", "run_finished"))
    return dict(last.payload_json or {}) if last is not None and last.kind == "stuck_ask" else None


def _budget_paused(session: Session, run_id: UUID) -> bool:
    last = _last(session, run_id, ("budget_ask", "budget_raised", "run_resumed", "run_finished"))
    return last is not None and last.kind == "budget_ask"


# A task's status on its card: the tone and the word for it.
STATUS = {
    "PENDING": ("waiting", "Queued"),
    "RUNNING": ("running", "Running"),
    "RECOVERING": ("running", "Picking up again"),
    "PAUSED": ("waiting", "Paused"),
    "WAITING_FOR_APPROVAL": ("waiting", "Waiting for your approval"),
    "NEEDS_HUMAN_REVIEW": ("waiting", "Needs your decision"),
    "COMPLETED": ("done", "Finished"),
    "FAILED": ("failed", "Failed"),
    "CANCELLED": ("info", "Stopped"),
}


def progress_card(
    session: Session, run: Run, link: str | None = None, now: datetime | None = None
) -> tuple[slack_format.Card, bool, bool]:
    """The task's card, whether it is working, and whether it is stuck."""
    task = session.get(Task, run.root_task_id)
    objective = ((task.objective if task else "") or "the task").strip().splitlines()[0][:120]
    order, done, build = _steps(session, run.id)
    status = str(run.status)
    marks = []
    for node in order:
        label = STEP.get(node, node.replace("_", " ").capitalize())
        if node in done:
            marks.append(f"✓ {label}")
        elif status == "RUNNING":
            marks.append(f"*→ {label}*")
        else:
            marks.append(label)
    stuck = stuck_on(session, run.id) if status == "PAUSED" else None
    tone, word = STATUS.get(status, ("info", status.replace("_", " ").capitalize()))
    if status == "RUNNING" and order:
        word = f"Running · step {len(order)}"
    lines = []
    if marks:
        lines.append("  ·  ".join(marks))
    if build and status in ACTIVE:
        lines.append(f"_{slack_format.escaped(build[:300])}_")
    running = status in ("PENDING", "RUNNING", "RECOVERING")
    if status == "PENDING":
        lines.append("It starts in a moment.")
    elif status == "RECOVERING":
        lines.append("Picking up where it was after a restart.")
    elif stuck is not None:
        tone, word = "waiting", "Stuck"
        what = slack_format.escaped(str(stuck.get("what") or "a step cannot get on"))
        brief = slack_format.escaped(str(stuck.get("brief") or "")[:200])
        lines.append(f"*Stuck:* {what}" + (f" (on: {brief})" if brief else ""))
        lines.append("Reply here with a hint and it goes on from there, or skip the step.")
    elif status == "PAUSED" and _budget_paused(session, run.id):
        word = "Paused at its limit"
        lines.append("Paused at its spending limit. Raise the limit in the office to go on.")
    elif status == "PAUSED":
        lines.append("Paused. Resume it in the office.")
    elif status == "WAITING_FOR_APPROVAL":
        lines.append("Waiting for your approval: see the message below.")
    elif status == "NEEDS_HUMAN_REVIEW":
        lines.append("Stopped and needs your decision in the office.")
    elif status == "COMPLETED":
        lines.append("✓ Finished. The answer is below.")
    elif status == "FAILED":
        why = stop_text((run.state_json or {}).get("stopped_reason") or run.error)
        lines.append(f"Failed: {slack_format.escaped(why)}")
    elif status == "CANCELLED":
        lines.append("Stopped.")
    card = slack_format.Card(
        kind="Agent task",
        title=objective,
        tone=tone,
        status=word,
        about=task_about(session, run),
        body="\n".join(lines),
        facts=task_facts(session, run, now),
        footer="_Reply in this thread to give the team a note._" if running else "",
        buttons=slack_format.task_buttons(str(run.id), link, running, stuck is not None),
    )
    return card, running, stuck is not None


def _candidates(session: Session, now: datetime) -> list[Run]:
    origin = Run.config_snapshot_json["origin"]
    return list(
        session.scalars(
            select(Run).where(
                origin["kind"].as_string() == "slack",
                or_(Run.status.in_(ACTIVE), Run.completed_at >= now - SETTLE),
            )
        )
    )


def _ensure_row(session: Session, run: Run, text: str) -> ChannelDelivery | None:

    found = session.scalar(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == key_of(run.id))
    )
    if found is not None:
        return found
    if run.status not in ACTIVE:
        return None  # Finished before it was ever shown: its result says it all.
    origin = (run.config_snapshot_json or {}).get("origin") or {}
    reply = origin_destination(session, origin)
    if reply is None:
        return None
    destination_id, app_id, agent_profile_id = reply
    row = ChannelDelivery(
        destination_id=destination_id,
        run_id=run.id,
        event_kind=EVENT,
        idempotency_key=key_of(run.id),
        text="",
        status="QUEUED",
        app_id=app_id,
        agent_profile_id=agent_profile_id,
        thread_ref=origin.get("thread"),
    )
    session.add(row)
    session.flush()
    return row


async def sync_progress(
    session_factory: sessionmaker[Session], settings: Settings, now: datetime | None = None
) -> int:
    """Post or edit each Slack task's progress message. Returns how many changed."""
    now = now or datetime.now(UTC)
    planned: list[tuple[UUID, str, list[dict[str, Any]]]] = []
    with session_factory() as session, session.begin():
        for run in _candidates(session, now):
            if ((run.config_snapshot_json or {}).get("origin") or {}).get("standing"):
                continue  # A standing report posts its result, not its steps.
            link = office_link(session, run.id)
            card, _, _ = progress_card(session, run, link, now)
            text = slack_format.card_text(card)
            row = _ensure_row(session, run, text)
            if row is None or row.status == "UNKNOWN":
                continue
            if row.status == "SENT" and row.text == text:
                continue
            if row.status != "SENT":
                if row.attempts >= MAX_ATTEMPTS:
                    continue
                # Recorded before sending, so a post whose outcome is unknown
                # can be found on Slack by its text.
                row.text = text
            planned.append((row.id, text, slack_format.card_blocks(card)))
    changed = 0
    for delivery_id, text, blocks in planned:
        with session_factory() as session:
            row = session.get(ChannelDelivery, delivery_id)
            if row is None:
                continue
            destination = session.get(ChannelDestination, row.destination_id)
            installation = (
                session.get(ChannelInstallation, destination.installation_id)
                if destination
                else None
            )
            posted, reference, thread = row.status == "SENT", row.external_ref, row.thread_ref
        if destination is None or installation is None:
            continue
        status, error = "SENT", None
        try:
            token = resolve_credential(installation.token_ref, settings)
            adapter = adapter_for(installation.kind)
            if posted and reference:
                channel, _, ts = reference.partition(":")
                await adapter.update(token, channel, ts, OutboundMessage(text=text, blocks=blocks))
            else:
                result = await adapter.send(
                    token,
                    destination.external_id,
                    OutboundMessage(text=text, thread=thread, blocks=blocks),
                )
                reference = result.external_ref
            changed += 1
        except SecretStoreError:
            status, error = ("SENT" if posted else "FAILED"), "credential_unavailable"
        except ChannelError as failure:
            if posted:
                # An edit that did not land is tried again next tick; the
                # message itself is already there.
                logger.warning('{"event":"progress_edit_failed","code":"%s"}', failure.code)
                continue
            status = "UNKNOWN" if failure.code == "outcome_unknown" else "FAILED"
            error = failure.code
        with session_factory() as session, session.begin():
            row = session.get(ChannelDelivery, delivery_id)
            if row is None:
                continue
            row.status = status
            row.error = error
            if status == "SENT" and error is None:
                row.text = text
                row.external_ref = reference
            if not posted:
                row.attempts += 1
    return changed
