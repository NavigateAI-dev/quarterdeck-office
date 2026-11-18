"""The office's projects as a request is routed to one of them.

A name alone says little: projects made from Slack are named after the first
question asked in them, and test projects share names. The judge choosing
where new work belongs is told what each project is about — what the owner
wrote of it, its business line, what was asked in it lately — so a related
question lands beside the work it relates to rather than in a new project of
its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Project, Run, Task, Venture
from app.titles import title_of

# How many projects the judge chooses among, most recently active first.
MOST = 25
RECENT_TASKS = 3
ABOUT_CHARS = 500
# What a project made from Slack says of itself: nothing about its work.
PLACEHOLDER = "Created from Slack."


@dataclass(frozen=True)
class Catalog:
    """What each project is about, and which project each label means."""

    described: dict[str, str]
    ids: dict[str, UUID]


def catalog(session: Session) -> Catalog:
    """Active projects, most recently active first, each with what it is about.

    Two projects sharing a name are told apart: the most recently active keeps
    the plain name, since that is the one a person means by it; the others
    are numbered.
    """
    last_task = (
        select(Run.project_id, func.max(Task.created_at).label("at"))
        .join(Task, Task.id == Run.root_task_id)
        .group_by(Run.project_id)
        .subquery()
    )
    rows = list(
        session.execute(
            select(Project, Venture.name, last_task.c.at)
            .outerjoin(Venture, Venture.id == Project.venture_id)
            .outerjoin(last_task, last_task.c.project_id == Project.id)
            .where(Project.status == "ACTIVE")
        ).all()
    )
    rows.sort(key=lambda row: _latest(row[2], row[0].created_at), reverse=True)
    described: dict[str, str] = {}
    ids: dict[str, UUID] = {}
    for project, venture, _ in rows[:MOST]:
        label = project.name
        copy = 2
        while label in ids:
            label = f"{project.name} (#{copy})"
            copy += 1
        ids[label] = project.id
        described[label] = _about(session, project, venture)
    return Catalog(described, ids)


def _latest(task_at: datetime | None, created: datetime | None) -> datetime:
    return task_at or created or datetime.min.replace(tzinfo=UTC)


def _about(session: Session, project: Project, venture: str | None) -> str:
    parts = []
    description = " ".join((project.description or "").split())
    if description and description != PLACEHOLDER:
        parts.append(description)
    if venture:
        parts.append(f"Business line: {venture}.")
    recent = session.execute(
        select(Task.title, Task.objective)
        .join(Run, Run.root_task_id == Task.id)
        .where(Run.project_id == project.id)
        .order_by(Task.created_at.desc())
        .limit(RECENT_TASKS)
    ).all()
    if recent:
        asked = "; ".join(title_of(title, objective)[:120] for title, objective in recent)
        parts.append(f"Recent tasks: {asked}.")
    return " ".join(parts)[:ABOUT_CHARS]
