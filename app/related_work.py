"""What the rest of the office already found that bears on a new task.

A task was told its own project's context and nothing else, and projects are
small: Slack makes one per question. A question about the same business asked
a day later, in another project, started from nothing.

When a task first starts, the office's recent finished answers, from every
project, are judged for relevance to its question, and the few that bear on it
are told to its agents as related work: what was asked, what was concluded,
where. Told as what was found then, never as settled fact, exactly as a
follow-up is told what the task before it found.

Relevance is judged (TypeSafe, which reads any language), not guessed from
shared words: the owner writes in Chinese and English, and a task handed
unrelated work is worse off than one handed none. Without the judgment nothing
is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OfficeSettings, Project, Run, Task
from app.follow_up import CARRIED, SUPPORTING, _as_lines, parent_answer
from app.judgments import JudgmentUnavailable
from app.titles import title_of

# How far back the office looks, and how much of it a task is told.
CANDIDATES = 30
MOST = 3
# On TypeSafe's scale of 0 (unrelated) to 3 (directly answers). Calibrated on
# the live office: work on the same subject scored 1.0 to 1.2 ("does not answer
# it, bears on it"), unrelated work 0.0 to 0.9.
RELEVANT = 1.0
ASKED_CHARS = 300
ANSWER_CHARS = 900
FINDINGS = 3
FINDING_CHARS = 200
# Tasks that concluded something the owner could read.
FINISHED = ("COMPLETED", "WAITING_FOR_APPROVAL", "CLOSED")


@dataclass(frozen=True)
class Found:
    """One earlier task's answer, as it would be told."""

    run_id: UUID
    text: str


def recent_answers(
    session: Session, store: Any, exclude: set[UUID], project_id: UUID | None = None
) -> list[Found]:
    """The office's most recent finished answers: from the asking project's own
    business line, or from every project when the owner allows it. Work
    for one line is not mixed into another's because they share an office."""
    query = (
        select(Run, Task.objective, Task.title, Project.name)
        .join(Task, Task.id == Run.root_task_id)
        .join(Project, Project.id == Run.project_id)
        .where(Run.status.in_(FINISHED))
    )
    across = session.scalar(select(OfficeSettings.related_across_lines).limit(1))
    asking = session.get(Project, project_id) if project_id is not None else None
    if asking is not None and not across:
        query = query.where(
            Project.venture_id == asking.venture_id
            if asking.venture_id is not None
            else Project.venture_id.is_(None)
        )
    rows = session.execute(
        query.order_by(Run.completed_at.desc().nulls_last()).limit(CANDIDATES + len(exclude))
    ).all()
    found = []
    asked: set[tuple[str, str]] = set()
    for run, objective, title, project in rows:
        if run.id in exclude:
            continue
        # The same question asked again in the same project is a rerun: its
        # newest answer speaks for it, rather than filling every place.
        key = (str(run.project_id), " ".join(objective.split()).lower())
        if key in asked:
            continue
        asked.add(key)
        payload = parent_answer(session, store, run)
        if payload is None:
            continue
        text = told(title_of(title, objective), objective, project, run.completed_at, payload)
        if text:
            found.append(Found(run.id, text))
        if len(found) >= CANDIDATES:
            break
    return found


def told(
    title: str, objective: str, project: str, finished: datetime | None, payload: dict[str, Any]
) -> str:
    """One earlier answer in a few lines: what was asked, concluded and found."""
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
    when = f", {finished.date().isoformat()}" if finished else ""
    lines = [
        f"«{title}» in {project}{when}. Asked: {objective.strip()[:ASKED_CHARS]}",
        f"Concluded: {answer[:ANSWER_CHARS]}",
    ]
    for key in SUPPORTING:
        values = [v for v in _as_lines(payload.get(key)) if not v.startswith("http")]
        if values and key == "findings":
            lines.append("Found: " + "; ".join(v[:FINDING_CHARS] for v in values[:FINDINGS]))
    return "\n".join(lines)


async def related_to(judge: Any, question: str, found: list[Found]) -> list[Found]:
    """The few answers that bear on this question, most relevant first."""
    if judge is None or not found:
        return []
    try:
        scores = await judge.relevance(question, [item.text for item in found])
    except JudgmentUnavailable:
        return []
    ranked = sorted(
        ((score, index) for index, score in enumerate(scores) if score >= RELEVANT),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [found[index] for _, index in ranked[:MOST]]
