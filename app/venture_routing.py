"""Sorting projects into the business lines they belong to.

The owner should not have to classify work by hand. A project's name, what the
owner wrote about it, and what its recent tasks actually asked for are enough
for Jev to say which line it belongs to, and how sure it is.

What happens next is not Jev's to decide. A confident answer is applied; an
unsure one is put to the owner; "none of these" is respected rather than forced
into the nearest line, because the wrong doctrine in front of an agent is worse
than none at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Project, Run, Task, Topic, Venture
from app.judgments import Judge, JudgmentUnavailable

# Below this the office asks rather than assigns. A project put under the wrong
# line is told the wrong standard of proof by every agent that works on it.
# Lowered from 0.75 at the owner's request, so more place themselves.
CONFIDENT = 0.6
RECENT_TASKS = 5
TASK_CHARS = 200
NONE = "none"
# Who put a project under its line. The office never moves the owner's choice,
# and "none" chosen by the owner is a choice too.
OWNER = "owner"
OFFICE = "office"
# Judged at most this many a pass, so a new office with many projects is
# sorted over a few passes rather than in one long call.
PER_PASS = 10


def line_options(session: Session) -> list[tuple[str, str]]:
    """The owner's business lines, as the choice Jev is offered."""
    rows = session.scalars(
        select(Venture).where(Venture.status == "ACTIVE").order_by(Venture.created_at)
    )
    options = []
    for row in rows:
        described = row.description or row.doctrine or "No description given."
        options.append((row.name, described[:600]))
    return options


def describe(session: Session, project: Project) -> dict[str, Any]:
    """What a project is, judged from its work rather than its label.

    A description can be empty or unhelpful — several of this office's projects
    say only "Created from Slack" — so the tasks it has actually run carry most
    of the signal.
    """
    objectives = session.scalars(
        select(Task.objective)
        .join(Run, Run.root_task_id == Task.id)
        .where(Run.project_id == project.id)
        .order_by(Run.started_at.desc().nullslast())
        .limit(RECENT_TASKS)
    )
    return {
        "name": project.name,
        "described_as": project.description or "",
        "recent_tasks": [str(text)[:TASK_CHARS] for text in objectives if text],
    }


def _not_settled_by_owner() -> Any:
    return or_(Project.venture_set_by.is_(None), Project.venture_set_by != OWNER)


def task_counts(session: Session, project_ids: list[UUID]) -> dict[UUID, int]:
    if not project_ids:
        return {}
    rows = session.execute(
        select(Run.project_id, func.count())
        .where(Run.project_id.in_(project_ids))
        .group_by(Run.project_id)
    )
    return {project_id: int(count) for project_id, count in rows}


async def sort_projects(
    session: Session,
    judge: Judge | None,
    project_ids: list[UUID] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Suggest a business line for each project that has none.

    Returns what was assigned and what the office could not decide, so the
    owner sees both rather than only the tidy half. Every judgment is kept on
    the project, so an unsure one can be shown as a suggestion.
    """
    lines = line_options(session)
    if judge is None or not lines:
        return {"assigned": [], "unsure": [], "reason": "no_judge" if not lines else "no_lines"}
    statement = select(Project).where(
        Project.status == "ACTIVE", Project.venture_id.is_(None), _not_settled_by_owner()
    )
    if project_ids:
        statement = statement.where(Project.id.in_(project_ids))
    projects = list(session.scalars(statement))
    if not projects:
        return {"assigned": [], "unsure": []}
    by_name = {
        row.name: row for row in session.scalars(select(Venture).where(Venture.status == "ACTIVE"))
    }
    try:
        answers = await judge.business_lines([describe(session, p) for p in projects], lines)
    except JudgmentUnavailable:
        return {"assigned": [], "unsure": [], "reason": "judge_unavailable"}

    judged_at = (now or datetime.now(UTC)).isoformat()
    counts = task_counts(session, [project.id for project in projects])
    assigned: list[dict[str, Any]] = []
    unsure: list[dict[str, Any]] = []
    for project, (chosen, confidence) in zip(projects, answers, strict=False):
        venture = by_name.get(chosen)
        record = {
            "project_id": str(project.id),
            "project": project.name,
            "line": chosen,
            "confidence": round(confidence, 3),
        }
        project.venture_suggestion_json = {
            "line": chosen,
            "venture_id": str(venture.id) if venture is not None else None,
            "confidence": round(confidence, 3),
            "judged_at": judged_at,
            "tasks": counts.get(project.id, 0),
        }
        if chosen == NONE or venture is None or confidence < CONFIDENT:
            unsure.append(record)
            continue
        project.venture_id = venture.id
        project.venture_set_by = OFFICE
        # Its discussions belong where it does.
        session.execute(
            update(Topic).where(Topic.project_id == project.id).values(venture_id=venture.id)
        )
        assigned.append(record | {"venture_id": str(venture.id)})
    session.flush()
    return {"assigned": assigned, "unsure": unsure}


def needs_judging(
    suggestion: dict[str, Any], tasks: int, lines_changed_at: datetime | None
) -> bool:
    """Judged again only for a reason: never judged, new work, or new lines."""
    if not suggestion:
        return True
    if suggestion.get("tasks") != tasks:
        return True
    try:
        judged_at = datetime.fromisoformat(str(suggestion.get("judged_at")))
    except ValueError:
        return True
    return lines_changed_at is not None and lines_changed_at > judged_at


async def auto_sort(
    session: Session, judge: Judge | None, now: datetime | None = None
) -> dict[str, Any]:
    """Sort what is waiting to be sorted, without the owner asking."""
    if judge is None:
        return {"assigned": [], "unsure": [], "reason": "no_judge"}
    candidates = list(
        session.execute(
            select(Project.id, Project.venture_suggestion_json).where(
                Project.status == "ACTIVE",
                Project.venture_id.is_(None),
                _not_settled_by_owner(),
            )
        )
    )
    if not candidates:
        return {"assigned": [], "unsure": []}
    counts = task_counts(session, [project_id for project_id, _ in candidates])
    lines_changed_at = session.scalar(select(func.max(Venture.updated_at)))
    due = [
        project_id
        for project_id, suggestion in candidates
        if needs_judging(dict(suggestion or {}), counts.get(project_id, 0), lines_changed_at)
    ][:PER_PASS]
    if not due:
        return {"assigned": [], "unsure": []}
    return await sort_projects(session, judge, due, now)


def line_suggestion(session: Session, project: Project) -> dict[str, Any]:
    """The office's last judgment, if it still points at a line that exists."""
    judged = dict(project.venture_suggestion_json or {})
    if not judged:
        return {}
    venture = None
    if judged.get("venture_id"):
        try:
            venture = session.get(Venture, UUID(str(judged["venture_id"])))
        except ValueError:
            venture = None
    usable = venture is not None and venture.status == "ACTIVE"
    return {
        "line": venture.name if usable and venture is not None else judged.get("line"),
        "venture_id": str(venture.id) if usable and venture is not None else None,
        "confidence": judged.get("confidence"),
        "judged_at": judged.get("judged_at"),
    }


def business_line(session: Session, project: Project) -> dict[str, Any]:
    """Which line a project's work is judged by, who put it there, and the
    office's suggestion when it has none."""
    venture = session.get(Venture, project.venture_id) if project.venture_id else None
    return {
        "project_id": str(project.id),
        "id": str(venture.id) if venture is not None else None,
        "name": venture.name if venture is not None else None,
        "set_by": project.venture_set_by,
        "suggestion": line_suggestion(session, project) if venture is None else {},
    }
