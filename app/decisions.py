"""Decisions only from the owner, and business spaces.

A passing idea is not a strategy. "Maybe agency-first" said on Tuesday must not
become what every agent follows on Wednesday. A decision is recorded only when
the owner says so (Record as decision), with the message it came from; a new
one may replace an earlier one, which is kept, marked replaced, and no longer
told to agents. A business space shows its decisions with that history, what
it is working towards, its discussions and projects, and its materials.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Decision,
    KnowledgeSource,
    Objective,
    Project,
    ProjectSource,
    RoomMessage,
    Topic,
    Venture,
)
from app.topics import clip_text, listed, touch

TITLE_CHARS = 120


class PlaceFirst(Exception):
    """A decision needs a project to hold it; the discussion belongs to none."""


class NotReplaceable(Exception):
    """The decision named to be replaced is not one this one can replace."""


def record(
    session: Session,
    topic: Topic,
    message: RoomMessage,
    text: str,
    owner: str,
    replaces: UUID | None = None,
) -> Decision:
    """The owner's decision, from their own words, in the topic's project."""
    if topic.project_id is None:
        raise PlaceFirst(str(topic.id))
    earlier = None
    if replaces is not None:
        earlier = session.get(Decision, replaces)
        if (
            earlier is None
            or earlier.status != "active"
            or not _same_space(session, earlier, topic)
        ):
            raise NotReplaceable(str(replaces))
    decision = Decision(
        project_id=topic.project_id,
        title=clip_text(text, TITLE_CHARS),
        decision=text,
        reason=f"Said by the owner in “{topic.title}”.",
        confidence="owner",
        approved_by=owner,
        topic_id=topic.id,
        source_message_id=message.id,
        status="active",
        metadata_json={"source": "discussion"},
    )
    session.add(decision)
    session.flush()
    if earlier is not None:
        earlier.status = "replaced"
        earlier.replaced_by_id = decision.id
        earlier.replaced_at = datetime.now(UTC)
    project = session.get(Project, topic.project_id)
    said = f"Recorded as a decision for {project.name if project else 'this project'}: {text}"
    if earlier is not None:
        said += f" It replaces: “{clip_text(earlier.decision, 200)}”, which is kept as history."
    session.add(
        RoomMessage(
            project_id=topic.project_id,
            topic_id=topic.id,
            author_kind="office",
            author_name="The office",
            body=said,
            reply_to=message.id,
        )
    )
    touch(session, topic.id)
    session.flush()
    return decision


def _same_space(session: Session, decision: Decision, topic: Topic) -> bool:
    """A decision may replace one of its own project, or of its business line."""
    if decision.project_id == topic.project_id:
        return True
    theirs = session.get(Project, decision.project_id)
    return bool(topic.venture_id) and theirs is not None and theirs.venture_id == topic.venture_id


def view(session: Session, decision: Decision) -> dict[str, Any]:
    topic = session.get(Topic, decision.topic_id) if decision.topic_id else None
    return {
        "id": str(decision.id),
        "text": decision.decision,
        "status": decision.status,
        "by": decision.approved_by,
        "at": decision.created_at.isoformat() if decision.created_at else None,
        "project_id": str(decision.project_id),
        "source": {
            "topic_id": str(topic.id),
            "title": topic.title,
            "message_id": f"m:{decision.source_message_id}" if decision.source_message_id else None,
        }
        if topic is not None
        else None,
        "replaced_at": decision.replaced_at.isoformat() if decision.replaced_at else None,
    }


def in_force(session: Session, project_ids: list[UUID]) -> list[dict[str, Any]]:
    """Decisions in force for these projects, newest first, each with the
    decisions it replaced, oldest replaced last."""
    rows = list(
        session.scalars(
            select(Decision)
            .where(Decision.project_id.in_(project_ids))
            .order_by(Decision.created_at.desc())
        )
    )
    by_replacer: dict[UUID, list[Decision]] = {}
    for row in rows:
        if row.replaced_by_id is not None:
            by_replacer.setdefault(row.replaced_by_id, []).append(row)
    shown = []
    for row in rows:
        if row.status != "active":
            continue
        chain: list[Decision] = []
        frontier = [row.id]
        while frontier:
            older = [d for key in frontier for d in by_replacer.get(key, [])]
            chain += older
            frontier = [d.id for d in older]
        shown.append({**view(session, row), "replaced": [view(session, d) for d in chain]})
    return shown


def for_topic(session: Session, topic: Topic) -> list[dict[str, Any]]:
    """The decisions in force where this discussion belongs."""
    if topic.project_id is None:
        return []
    return in_force(session, [topic.project_id])


def space(session: Session, venture: Venture) -> dict[str, Any]:
    """A business line as one place: decisions, goals, discussions, projects,
    materials."""
    projects = list(
        session.scalars(
            select(Project)
            .where(Project.venture_id == venture.id, Project.status == "ACTIVE")
            .order_by(Project.name)
        )
    )
    ids = [project.id for project in projects]
    goals = [
        {
            "id": str(goal.id),
            "title": goal.title,
            "measure": goal.measure,
            "status": goal.status,
            "ends_on": goal.ends_on.isoformat() if goal.ends_on else None,
        }
        for goal in session.scalars(
            select(Objective)
            .where(Objective.venture_id == venture.id)
            .order_by(Objective.created_at)
        )
    ]
    materials = (
        [
            {"id": str(source.id), "name": source.name, "documents": source.document_count}
            for source in session.scalars(
                select(KnowledgeSource)
                .join(ProjectSource, ProjectSource.source_id == KnowledgeSource.id)
                .where(ProjectSource.project_id.in_(ids))
                .distinct()
            )
        ]
        if ids
        else []
    )
    talks = [topic for topic in listed(session, None) if topic.get("business_line") == venture.name]
    return {
        "id": str(venture.id),
        "name": venture.name,
        "description": venture.description,
        "doctrine": venture.doctrine,
        "decisions": in_force(session, ids) if ids else [],
        "goals": goals,
        "discussions": talks,
        "projects": [{"id": str(p.id), "name": p.name} for p in projects],
        "materials": materials,
    }
