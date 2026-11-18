"""Topics: talk first, and let work form in the conversation.

A topic is the one conversation object of the office. It can start from a
sentence with no project, no task and no form; it may later be placed in a
project or a business line, by the office from what was said or by the owner.
A project's room is its first topic (`home`). Tasks started from a topic
remember it, and report back into it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app import ideas
from app.db.models import AgentProfile, Project, RoomMessage, Run, Topic, Venture
from app.titles import title_for
from app.topic_briefs import welcome_back

STATUSES = ("open", "resting", "archived")
MAX_PARTICIPANTS = 6
LISTED = 60
EXCERPT = 160


def home_topic(session: Session, project: Project, create: bool = True) -> Topic | None:
    """The project's first topic, which is what its room was."""
    topic = session.scalar(select(Topic).where(Topic.project_id == project.id, Topic.home))
    if topic is None and create:
        topic = Topic(
            title=project.name[:200],
            project_id=project.id,
            venture_id=project.venture_id,
            scope_set_by="owner",
            home=True,
        )
        session.add(topic)
        session.flush()
    return topic


def start(session: Session, text: str, project: Project | None = None) -> Topic:
    """A new topic from what the owner said; titled from it, in code."""
    topic = Topic(
        title=(title_for(text) or "A new topic")[:200],
        project_id=project.id if project is not None else None,
        venture_id=project.venture_id if project is not None else None,
        scope_set_by="owner" if project is not None else None,
        home=False,
    )
    session.add(topic)
    session.flush()
    return topic


def touch(session: Session, topic_id: UUID | None) -> None:
    """Something was said or done in the topic: it is the most recent now."""
    if topic_id is None:
        return
    topic = session.get(Topic, topic_id)
    if topic is not None:
        topic.last_activity_at = datetime.now(UTC)
        if topic.status == "resting":
            topic.status = "open"


def runs_of(topic: Topic) -> ColumnElement[bool]:
    """Which tasks a topic shows: its own, and for a project's first topic,
    the project's tasks that no other topic started."""
    if topic.home and topic.project_id is not None:
        return (Run.project_id == topic.project_id) & (
            Run.topic_id.is_(None) | (Run.topic_id == topic.id)
        )
    return Run.topic_id == topic.id


def set_scope(session: Session, topic: Topic, project_id: UUID | None, by: str = "owner") -> Topic:
    """Place the topic in a project (and so its business line), or free it.
    A project's first topic stays in its project."""
    if topic.home:
        return topic
    project = session.get(Project, project_id) if project_id is not None else None
    topic.project_id = project.id if project is not None else None
    topic.venture_id = project.venture_id if project is not None else None
    topic.scope_set_by = by if project is not None else None
    session.flush()
    return topic


def view(session: Session, topic: Topic) -> dict[str, Any]:
    """A topic as the screens show it."""
    project = session.get(Project, topic.project_id) if topic.project_id else None
    venture = session.get(Venture, topic.venture_id) if topic.venture_id else None
    last = session.scalar(
        select(RoomMessage)
        .where(RoomMessage.topic_id == topic.id, RoomMessage.thread_id.is_(None))
        .order_by(RoomMessage.id.desc())
        .limit(1)
    )
    said = session.scalar(
        select(func.count(RoomMessage.id)).where(RoomMessage.topic_id == topic.id)
    )
    working = session.scalar(
        select(func.count(Run.id)).where(
            runs_of(topic), Run.status.in_(("PENDING", "RUNNING", "RECOVERING"))
        )
    )
    return {
        "id": str(topic.id),
        "title": topic.title,
        "home": topic.home,
        "project": {"id": str(project.id), "name": project.name} if project else None,
        "business_line": venture.name if venture else None,
        "scope_set_by": topic.scope_set_by,
        "status": topic.status,
        "parent_topic_id": str(topic.parent_topic_id) if topic.parent_topic_id else None,
        "brief": dict(topic.brief_json or {}),
        "revisit_at": topic.revisit_at.isoformat() if topic.revisit_at else None,
        "last_activity_at": topic.last_activity_at.isoformat() if topic.last_activity_at else None,
        "messages": int(said or 0),
        # Colleagues brought in, in the order they joined.
        "participants": participants(session, topic),
        "welcome_back": welcome_back(topic),
        "parent": _link(session, topic.parent_topic_id),
        "branches": [
            {"id": str(child.id), "title": child.title}
            for child in session.scalars(
                select(Topic).where(Topic.parent_topic_id == topic.id).order_by(Topic.created_at)
            )
        ],
        # Ideas noted here and not taken up yet.
        "ideas": ideas.of_topic(session, topic.id),
        "working": int(working or 0),
        "last": {
            "author": last.author_name,
            "kind": last.author_kind,
            "text": " ".join(last.body.split())[:EXCERPT],
        }
        if last is not None
        else None,
    }


def _link(session: Session, topic_id: UUID | None) -> dict[str, str] | None:
    other = session.get(Topic, topic_id) if topic_id is not None else None
    return {"id": str(other.id), "title": other.title} if other is not None else None


def participants(session: Session, topic: Topic) -> list[dict[str, Any]]:
    """The colleagues brought into the discussion, with their names now."""
    ids = [str(value) for value in topic.participants_json or []]
    if not ids:
        return []
    rows = {
        str(row.id): row
        for row in session.scalars(select(AgentProfile).where(AgentProfile.id.in_(ids)))
    }
    return [
        {"profile_id": pid, "name": rows[pid].name, "role": rows[pid].role}
        for pid in ids
        if pid in rows
    ]


def join(session: Session, topic: Topic, profile_ids: list[str]) -> list[str]:
    """Bring colleagues in; returns those newly joined, in order."""
    held = [str(value) for value in topic.participants_json or []]
    added = [pid for pid in profile_ids if pid and pid not in held][:MAX_PARTICIPANTS]
    if added:
        topic.participants_json = (held + added)[:MAX_PARTICIPANTS]
        session.flush()
    return [pid for pid in added if pid in (topic.participants_json or [])]


def leave(session: Session, topic: Topic, profile_id: str) -> None:
    topic.participants_json = [
        str(value) for value in topic.participants_json or [] if str(value) != profile_id
    ]
    session.flush()


def branch(session: Session, parent: Topic, message: RoomMessage, title: str, brief: str) -> Topic:
    """A tangent as a discussion of its own: same project, a carried brief
    instead of the whole history, and links both ways."""
    child = Topic(
        title=title[:200],
        project_id=parent.project_id,
        venture_id=parent.venture_id,
        scope_set_by=parent.scope_set_by,
        home=False,
        parent_topic_id=parent.id,
        branched_from_message_id=message.id,
        brief_json={"where": brief[:220], "decided": "", "open": ""} if brief else {},
    )
    session.add(child)
    session.flush()
    carried = f"Branched from “{parent.title}”, on: “{clip_text(message.body, 300)}”"
    if brief:
        carried += f"\n\nCarried over: {brief}"
    session.add(
        RoomMessage(
            project_id=child.project_id,
            topic_id=child.id,
            author_kind="office",
            author_name="The office",
            body=carried,
        )
    )
    session.add(
        RoomMessage(
            project_id=parent.project_id,
            topic_id=parent.id,
            author_kind="office",
            author_name="The office",
            body=f"Branched into “{child.title}”. This discussion carries on here.",
            reply_to=message.id,
            thread_id=message.thread_id,
            actions_json=[{"kind": "open_topic", "topic_id": str(child.id)}],
        )
    )
    touch(session, parent.id)
    touch(session, child.id)
    session.flush()
    return child


def clip_text(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def listed(session: Session, status: str | None = "open") -> list[dict[str, Any]]:
    """The topics with something said in them, most recent first."""
    query = select(Topic).where(
        select(RoomMessage.id).where(RoomMessage.topic_id == Topic.id).exists()
    )
    if status is not None:
        query = query.where(Topic.status == status)
    rows = session.scalars(query.order_by(Topic.last_activity_at.desc()).limit(LISTED))
    return [view(session, topic) for topic in rows]
