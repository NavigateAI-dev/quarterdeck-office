"""A task's lineage: how one piece of work led to another.

Read from what the office already records, never kept in a second store that
could disagree with it: the task a task continues (`runs.continues_run_id`)
and the tasks that continue it, the related work it was told about and the
later tasks told about it (`related_work` events), the lessons it taught
(`project_memory.run_id`), the facts it noted about its subjects
(`entity_facts.run_id`), the objective it serves, and the room questions
asked about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Entity,
    EntityFact,
    Objective,
    Project,
    ProjectMemory,
    RoomMessage,
    Run,
    RunEvent,
    Task,
)
from app.follow_up import CARRIED, parent_answer
from app.judgments import JudgmentUnavailable
from app.related_work import RELEVANT
from app.titles import title_of

# How far a chain is followed each way. A longer line of work is rare, and a
# cycle (which the data should never hold) cannot loop past it.
MAX_HOPS = 6
LISTED = 20


def _run_view(session: Session, run_id: UUID, hops: int | None = None) -> dict[str, Any] | None:
    row = session.execute(
        select(Run, Task.objective, Task.title, Project.name)
        .join(Task, Task.id == Run.root_task_id)
        .join(Project, Project.id == Run.project_id, isouter=True)
        .where(Run.id == run_id)
    ).first()
    if row is None:
        return None
    run, objective, title, project = row
    view = {
        "run_id": str(run.id),
        "title": title_of(title, objective),
        "status": run.status,
        "project": project,
        "project_id": str(run.project_id) if run.project_id else None,
    }
    if hops is not None:
        view["hops"] = hops
    return view


def ancestors(session: Session, run_id: UUID, limit: int = MAX_HOPS) -> list[tuple[UUID, int]]:
    """The tasks this one continues, nearest first, with how many hops away."""
    found: list[tuple[UUID, int]] = []
    seen = {run_id}
    current = session.get(Run, run_id)
    hops = 0
    while current is not None and current.continues_run_id and hops < limit:
        parent = current.continues_run_id
        if parent in seen:
            break
        hops += 1
        seen.add(parent)
        found.append((parent, hops))
        current = session.get(Run, parent)
    return found


def descendants(session: Session, run_id: UUID) -> list[tuple[UUID, int]]:
    """The tasks that continue this one, at any depth, nearest first."""
    found: list[tuple[UUID, int]] = []
    seen = {run_id}
    frontier = [run_id]
    for hops in range(1, MAX_HOPS + 1):
        if not frontier:
            break
        children = list(
            session.scalars(
                select(Run.id)
                .where(Run.continues_run_id.in_(frontier))
                .order_by(Run.started_at.asc().nulls_last(), Run.id)
            )
        )
        frontier = [child for child in children if child not in seen]
        seen.update(frontier)
        found += [(child, hops) for child in frontier]
    return found


def told_about(session: Session, run_id: UUID) -> list[UUID]:
    """The earlier answers this task was told as related work."""
    ids: list[UUID] = []
    for payload in session.scalars(
        select(RunEvent.payload_json).where(
            RunEvent.run_id == run_id, RunEvent.kind == "related_work"
        )
    ):
        for value in (payload or {}).get("runs") or []:
            try:
                parsed = UUID(str(value))
            except ValueError:
                continue
            if parsed not in ids:
                ids.append(parsed)
    return ids


def told_to(session: Session, run_id: UUID) -> list[UUID]:
    """The later tasks this one was told to as related work."""
    # JSONB containment: the events whose list of runs holds this one.
    rows = session.scalars(
        select(RunEvent.run_id)
        .where(
            RunEvent.kind == "related_work",
            RunEvent.payload_json.contains({"runs": [str(run_id)]}),
        )
        .order_by(RunEvent.id)
    )
    ids: list[UUID] = []
    for other in rows:
        if other not in ids:
            ids.append(other)
    return ids


def lineage(session: Session, run_id: UUID) -> dict[str, Any] | None:
    """Everything this task is linked to; None when there is no such task."""
    run = session.get(Run, run_id)
    if run is None:
        return None

    def views(pairs: list[tuple[UUID, int]]) -> list[dict[str, Any]]:
        listed = [_run_view(session, other, hops) for other, hops in pairs[:LISTED]]
        return [view for view in listed if view is not None]

    def plain(ids: list[UUID]) -> list[dict[str, Any]]:
        listed = [_run_view(session, other) for other in ids[:LISTED]]
        return [view for view in listed if view is not None]

    taught = [
        {"text": row.lesson, "status": row.status, "kind": row.kind}
        for row in session.scalars(
            select(ProjectMemory)
            .where(ProjectMemory.run_id == run_id)
            .order_by(ProjectMemory.created_at)
        )
    ]
    noted = [
        {"subject": name, "text": fact.fact, "status": fact.status}
        for fact, name in session.execute(
            select(EntityFact, Entity.name)
            .join(Entity, Entity.id == EntityFact.entity_id)
            .where(EntityFact.run_id == run_id)
            .order_by(EntityFact.created_at)
        )
    ]
    serves = None
    if run.objective_id is not None:
        objective = session.get(Objective, run.objective_id)
        serves = objective.title if objective is not None else None
    asked = session.scalar(
        select(func.count(RoomMessage.id)).where(
            RoomMessage.run_id == run_id, RoomMessage.author_kind == "owner"
        )
    )
    return {
        "run_id": str(run_id),
        "built_on": views(ancestors(session, run_id)),
        "built_upon": views(descendants(session, run_id)),
        "told_about": plain(told_about(session, run_id)),
        "told_to": plain(told_to(session, run_id)),
        "taught": taught,
        "noted": noted,
        "serves": serves,
        "asked_about": int(asked or 0),
    }


# Earlier links of a line of work, told to a follow-up.
#
# A follow-up is told its parent's answer whole (app/follow_up.py). Further back
# the line is told only in part: each earlier link in a line or two, only if a
# relevance judgment says it bears on the new question, nearest first, within
# a budget. A fifth follow-up handed four ancestors' answers whole would be a
# task whose context is mostly archaeology; handed nothing, it forgets what
# the line established two steps ago.
LINE_HOPS = 4
LINE_CHARS = 2400
LINE_ASKED = 200
LINE_ANSWER = 400


@dataclass(frozen=True)
class Candidate:
    """One earlier link of a line of work, as it would be told."""

    run_id: UUID
    hops: int
    text: str


def line_candidates(
    session: Session, store: Any, run_id: UUID, exclude: set[UUID]
) -> list[Candidate]:
    """The earlier links of this task's line of work: its ancestors beyond the
    task it continues, and the related work that task was told about."""
    run = session.get(Run, run_id)
    if run is None or run.continues_run_id is None:
        return []
    parent = run.continues_run_id
    skip = set(exclude) | {run_id, parent}
    links: list[tuple[UUID, int, str]] = [
        (other, hops, f"{hops - 1} task{'s' if hops > 2 else ''} before the one it continues")
        for other, hops in ancestors(session, run_id, LINE_HOPS + 1)
        if hops >= 2
    ]
    links += [(other, 2, "told to the task it continues") for other in told_about(session, parent)]
    found: list[Candidate] = []
    for other, hops, where in links:
        if other in skip:
            continue
        skip.add(other)
        text = _link_text(session, store, other, where)
        if text:
            found.append(Candidate(other, hops, text))
    return found


def _link_text(session: Session, store: Any, run_id: UUID, where: str) -> str:
    row = session.execute(
        select(Run, Task.objective, Task.title)
        .join(Task, Task.id == Run.root_task_id)
        .where(Run.id == run_id)
    ).first()
    if row is None:
        return ""
    run, objective, title = row
    payload = parent_answer(session, store, run)
    if payload is None:
        return ""
    answer = next(
        (
            str(payload[key]).strip()
            for key in CARRIED
            if isinstance(payload.get(key), str) and str(payload[key]).strip()
        ),
        "",
    )
    if not answer:
        return ""
    asked = " ".join(objective.split())[:LINE_ASKED]
    return (
        f"«{title_of(title, objective)}» ({where}). Asked: {asked}. "
        f"Concluded: {' '.join(answer.split())[:LINE_ANSWER]}"
    )


async def judged_line(judge: Any, question: str, links: list[Candidate]) -> list[Candidate]:
    """The links that bear on this question, nearest first, within the budget.
    Without the judgment, nothing: unrelated context is the complaint."""
    if judge is None or not links:
        return []
    try:
        scores = await judge.relevance(question, [link.text for link in links])
    except JudgmentUnavailable:
        return []
    ranked = sorted(
        (
            (link.hops, -score, index, link)
            for index, (link, score) in enumerate(zip(links, scores, strict=False))
            if score >= RELEVANT
        ),
        key=lambda entry: entry[:3],
    )
    told: list[Candidate] = []
    used = 0
    for _, _, _, link in ranked:
        if used + len(link.text) > LINE_CHARS:
            continue
        told.append(link)
        used += len(link.text)
    return told
