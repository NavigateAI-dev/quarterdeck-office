"""A discussion's brief: where we were, what is decided, what is open.

Coming back to a discussion should not mean rereading it. When a discussion
goes quiet with something new said, its lead writes three short lines about
it, once, on its own model; the call's cost counts against the month like
every other. The brief is what the owner sees on coming back and on the desk,
what a colleague brought in is given instead of the whole history, and what a
branch carries from where it came from.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_profiles import capture_agent_snapshot
from app.db.models import RoomMessage, Topic, TopicItem
from app.office_settings import MonthlyLimitReached, check_monthly_limit, paused

logger = logging.getLogger(__name__)


def clip(text: str, limit: int) -> str:
    """Shorten at the limit, and say so."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Quiet for this long, with something new said, and it is briefed.
QUIET = timedelta(minutes=10)
# Fewer messages than this need no brief: they are their own summary.
FEWEST = 3
LINES = 40
PER_PASS = 5
FIELD_CHARS = 220
# Coming back after this long shows the brief first.
AWAY = timedelta(hours=2)

ASK = """You keep the brief of a discussion between the owner of a private office
and its agents. Write it for the owner coming back to it later: three short
lines, plain sentences, no Markdown.
- where: where the discussion got to, and any work under way.
- decided: only what the owner decided, said plainly; "Nothing yet." if nothing.
- open: the question still to settle; "Nothing open." if none.
Return JSON only: {"where": "...", "decided": "...", "open": "..."}."""


def last_message_id(session: Session, topic_id: UUID) -> int:
    found = session.scalar(select(func.max(RoomMessage.id)).where(RoomMessage.topic_id == topic_id))
    return int(found or 0)


def quiet(session: Session, now: datetime) -> list[UUID]:
    """Discussions gone quiet with something said since their last brief."""
    candidates = session.scalars(
        select(Topic)
        .where(Topic.status == "open", Topic.last_activity_at < now - QUIET)
        .order_by(Topic.last_activity_at.desc())
        .limit(50)
    )
    due: list[UUID] = []
    for topic in candidates:
        through = int((topic.brief_json or {}).get("through_message_id") or 0)
        said = session.scalar(
            select(func.count(RoomMessage.id)).where(RoomMessage.topic_id == topic.id)
        )
        if int(said or 0) >= FEWEST and last_message_id(session, topic.id) > through:
            due.append(topic.id)
    return due


def _parsed(text: str) -> dict[str, str] | None:
    found = re.search(r"\{.*\}", text, re.DOTALL)
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    brief = {
        key: clip(str(data.get(key) or "").strip(), FIELD_CHARS)
        for key in ("where", "decided", "open")
    }
    return brief if brief["where"] else None


def lead_of(agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Who answers a message that names no one: the Chief, else the planner."""
    for role in ("chief", "planner"):
        for agent in agents:
            if agent.get("role") == role:
                return agent
    return agents[0] if agents else None


def lead(session: Session) -> dict[str, Any] | None:
    agents = [dict(entry) for entry in capture_agent_snapshot(session)["agents"]]
    return lead_of(agents)


async def refresh(
    session_factory: sessionmaker[Session],
    topic_id: UUID,
    complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]],
) -> dict[str, Any] | None:
    """Write the discussion's brief now, on its lead's model. None if it could not."""
    with session_factory() as session:
        topic = session.get(Topic, topic_id)
        agent = lead(session)
        if topic is None or agent is None:
            return None
        rows = list(
            session.scalars(
                select(RoomMessage)
                .where(RoomMessage.topic_id == topic_id)
                .order_by(RoomMessage.id.desc())
                .limit(LINES)
            )
        )
        through = rows[0].id if rows else 0
        lines = [f"{row.author_name}: {clip(row.body, 500)}" for row in reversed(rows)]
        title = topic.title
    messages = [
        {"role": "system", "content": ASK},
        {
            "role": "user",
            "content": f"The discussion: {title}\n\n" + "\n".join(lines),
        },
    ]
    completion = await complete(agent, messages)
    brief = _parsed(str(completion.content or ""))
    usage = completion.usage
    with session_factory() as session, session.begin():
        session.add(
            TopicItem(
                topic_id=topic_id,
                kind="brief",
                text=json.dumps(brief or {"unreadable": True}),
                status="kept",
                cost_usd=completion.estimated_cost,
                tokens=int((usage.prompt_tokens or 0) + (usage.completion_tokens or 0)),
            )
        )
        if brief is None:
            return None
        topic = session.get(Topic, topic_id)
        if topic is not None:
            topic.brief_json = {
                **brief,
                "through_message_id": through,
                "at": datetime.now(UTC).isoformat(),
            }
    return brief


async def brief_pass(
    session_factory: sessionmaker[Session],
    settings: Any,
    complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]],
) -> int:
    """Brief the discussions gone quiet, a few at a time, unless chat is off or
    the month's money is spent."""

    with session_factory() as session:
        if paused(session, settings, "chat"):
            return 0
        try:
            check_monthly_limit(session, settings)
        except MonthlyLimitReached:
            return 0
        due = quiet(session, datetime.now(UTC))[:PER_PASS]
    done = 0
    for topic_id in due:
        try:
            if await refresh(session_factory, topic_id, complete) is not None:
                done += 1
        except Exception:
            logger.exception('{"event":"brief_failed","topic":"%s"}', topic_id)
    return done


def welcome_back(topic: Topic, now: datetime | None = None) -> bool:
    """Whether opening the discussion now should start with its brief."""
    if not (topic.brief_json or {}).get("where"):
        return False
    seen = topic.owner_seen_at
    return seen is None or seen < (now or datetime.now(UTC)) - AWAY
