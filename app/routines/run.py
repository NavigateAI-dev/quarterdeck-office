"""Firing routines: a due routine is a message its agent answers in its discussion.

The reply is an ordinary reply, with what that agent knows there and within the
chat allowance and the month's limit, so a routine is as good as asking in person.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import topics
from app.channels import slack_format
from app.db.models import RoomMessage, Schedule, Topic
from app.followups import service as followups
from app.office_settings import paused
from app.routines import service
from app.scheduling import next_occurrence

logger = logging.getLogger("quarterdeck.routines")
# Slack brings a channel message to the bot only when it is mentioned.
HOW_TO_ANSWER = "To answer, mention me in a reply here or send me a direct message."


def due(session: Session, now: datetime) -> list[UUID]:
    return list(
        session.scalars(
            select(Schedule.id).where(
                Schedule.prompt.is_not(None),
                Schedule.enabled.is_(True),
                Schedule.next_run_at <= now,
            )
        )
    )


def _asked_now(session: Session, row: Schedule, now: datetime) -> str | None:
    """What to ask this time. A Progress check with nothing due asks nothing."""
    if row.preset == followups.PRESET:
        return followups.check_in(session, row, now)
    asked = service.asked_of(row)
    if row.preset in followups.RECAPS:
        words = followups.recap_words(session, now)
        if words:
            return f"{asked}\n\n{words}"
    return asked


def post_asked(session: Session, row: Schedule, now: datetime) -> int | None:
    """The message the agent will answer, kept in the routine's discussion."""
    body = _asked_now(session, row, now)
    if body is None:
        row.last_outcome = "quiet"
        return None
    topic = session.get(Topic, row.topic_id)
    message = RoomMessage(
        # The reply reads its project from this: it decides which tools it may use.
        project_id=topic.project_id if topic is not None else None,
        topic_id=row.topic_id,
        author_kind="office",
        author_name="The office",
        body=body,
        client_key=f"routine:{row.id}:{int(now.timestamp())}",
    )
    session.add(message)
    topics.touch(session, row.topic_id)
    session.flush()
    row.last_message_id = message.id
    row.last_outcome = "started"
    return int(message.id)


def claim(session: Session, routine_id: UUID, settings: Any, now: datetime) -> int | None:
    """Take this occurrence, once. The message to answer, or None when not made."""
    row = session.scalar(select(Schedule).where(Schedule.id == routine_id).with_for_update())
    if row is None or not row.enabled or row.next_run_at is None or row.next_run_at > now:
        return None
    outcome = "started"
    if row.topic_id is None:
        outcome = "disabled"
    elif now - row.next_run_at > service.LATE_LIMIT:
        outcome = "skipped_too_late"
    elif paused(session, settings, "schedules"):
        outcome = "skipped_paused"
    if outcome == "disabled":
        row.enabled = False
        row.disabled_reason = "discussion_deleted"
        row.last_outcome = outcome
        return None
    service.say_missed(session, row, outcome)
    message = post_asked(session, row, now) if outcome == "started" else None
    row.last_outcome = "quiet" if outcome == "started" and message is None else outcome
    row.next_run_at = next_occurrence(service.rule_of(row), now)
    if row.next_run_at is None:
        row.enabled = False
        row.disabled_reason = "finished"
    return message


def agents_for(replier: Any, row_agent: UUID | None) -> list[dict[str, Any]]:
    """Who answers: the agent it was made with, else the lead."""
    named, _, _ = replier.who("", str(row_agent) if row_agent else None)
    return list(named)


def deliver(session: Session, routine_id: UUID, message_id: int) -> str:
    """What came back goes to the channel too, unless there was nothing to tell."""
    row = session.get(Schedule, routine_id)
    reply = session.scalar(
        select(RoomMessage)
        .where(RoomMessage.reply_to == message_id)
        .order_by(RoomMessage.id.desc())
        .limit(1)
    )
    if row is None:
        return "gone"
    if reply is None:
        row.last_outcome = "no_answer"
        return "no_answer"
    if reply.body.strip().upper().startswith(service.QUIET):
        row.last_outcome = "quiet"
        return "quiet"
    text = f"*{slack_format.escaped(str(row.title))}*\n\n{slack_format.mrkdwn(reply.body)}"
    if row.preset == followups.PRESET:
        text += f"\n\n_{HOW_TO_ANSWER}_"
    service.post_to_channel(
        session, row, text, service.EVENT, f"routine:{row.id}:{message_id}", formatted=True
    )
    row.last_outcome = "delivered" if reply.author_kind == "agent" else "not_answered"
    if row.preset in followups.RECAPS and row.last_outcome == "delivered":
        asked = session.get(RoomMessage, message_id)
        if asked is not None:
            followups.mark_reported(session, asked.created_at, datetime.now(UTC))
    return row.last_outcome


async def fire(
    session_factory: sessionmaker[Session],
    replier: Any,
    routine_id: UUID,
    message_id: int,
) -> str:
    """The agent answers, then the answer is delivered."""
    with session_factory() as session:
        row = session.get(Schedule, routine_id)
        if row is None or row.topic_id is None:
            return "gone"
        topic_id, agent = row.topic_id, row.agent_profile_id
    await replier.respond(topic_id, message_id, agents_for(replier, agent), [], [])
    with session_factory() as session, session.begin():
        return deliver(session, routine_id, message_id)


def send_now(session_factory: sessionmaker[Session], routine_id: UUID, now: datetime) -> int:
    """The owner's press: make one now; its timing stays as it is."""
    with session_factory() as session, session.begin():
        row = service.found(session, routine_id)
        if row.topic_id is None:
            raise service.RoutineRejected("its discussion is gone")
        message = post_asked(session, row, now)
        if message is None:
            raise service.RoutineRejected("nothing is due to ask about")
        return message


# Answers underway; a reply can take minutes, and the scheduler keeps ticking.
_firing: set[asyncio.Task[Any]] = set()


async def routine_pass(
    session_factory: sessionmaker[Session], settings: Any, replier: Any, now: datetime
) -> int:
    """Start every routine whose time has come. Returns how many started."""
    with session_factory() as session:
        ids = due(session, now)
    started = 0
    for routine_id in ids:
        with session_factory() as session, session.begin():
            message = claim(session, routine_id, settings, now)
        if message is None:
            continue
        task = asyncio.create_task(fire(session_factory, replier, routine_id, message))
        _firing.add(task)
        task.add_done_callback(_firing.discard)
        started += 1
    return started
