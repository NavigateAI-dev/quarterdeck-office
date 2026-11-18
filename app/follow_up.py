"""Continuing a finished task with the next question.

Work does not arrive as single questions. It arrives as a question, an answer,
and then the real question — the one the answer provoked. The office had
nowhere to put that: a finished task was read-only, and starting a fresh task
meant starting from nothing, with the office rediscovering what it had just
established.

A follow-up is a task in its own right — it is planned, it costs money, it can
be refused by a limit like any other. What it inherits is what the parent
concluded: the answer, what that rested on, what was left unknown, and which
sources were already read. Inherited as *told*, not as assumed: the follow-up
is given the parent's findings as context and is free to contradict them, which
is the difference between carrying knowledge forward and carrying a conclusion
forward.

The chain is kept both ways so a thread can be read as a thread, and it is
readable however long it gets: only the nearest parent's answer is inherited,
because a fifth follow-up given four ancestors' answers is a task whose context
is mostly archaeology.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Artifact, Run, Task
from app.titles import title_of

# What a follow-up is told about the task it continues. Enough to stand on,
# short enough to leave room for its own work.
ANSWER_CLIP = 1200
CARRIED = ("recommendation", "verdict", "answer", "summary")
SUPPORTING = ("findings", "unknowns", "sources", "open_questions")
MAX_ITEMS = 6
ITEM_CLIP = 240
# Artifacts that hold what a task concluded, most conclusive first.
ANSWER_TYPES = ("synthesis", "strategy_proposal", "final_report", "research_report")


class NotFinished(ValueError):
    """A task that has not concluded anything to continue from."""


def parent_answer(session: Session, store: Any, run: Run) -> dict[str, Any] | None:
    """The payload of what the parent concluded, if it concluded anything."""
    # Newest version first: a revised or reworked answer supersedes the first.
    rows = list(
        session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run.id)
            .order_by(Artifact.version.desc(), Artifact.created_at.desc())
        )
    )
    for kind in ANSWER_TYPES:
        for row in rows:
            if row.type == kind:
                try:
                    body = store.read(row.project_id, row.id, row.version)
                except (OSError, ValueError):
                    continue
                import json

                try:
                    payload = json.loads(body)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    return payload
    return None


def carried_context(session: Session, store: Any, run: Run) -> list[str]:
    """What the follow-up is told about the task it continues.

    Lines, not a blob: this goes into the run context beside the project
    background and the approved lessons, and is read by an agent in a prompt.
    """
    task = session.get(Task, run.root_task_id)
    lines: list[str] = []
    asked = (task.objective if task else "").strip()
    if asked:
        lines.append(f"This task continues an earlier one, which was asked: {asked[:ANSWER_CLIP]}")
    payload = parent_answer(session, store, run)
    if payload is None:
        lines.append(
            "The earlier task produced no recorded answer, so nothing from it can be relied on."
        )
        return lines
    for key in CARRIED:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"What it concluded: {value.strip()[:ANSWER_CLIP]}")
            break
    for key in SUPPORTING:
        values = _as_lines(payload.get(key))
        if values:
            lines.append(f"{key.replace('_', ' ').capitalize()} it recorded:")
            lines += [f"- {value}" for value in values[:MAX_ITEMS]]
    lines.append(
        "Treat all of the above as what was found last time, not as settled fact: say so if "
        "this task's own work contradicts it."
    )
    return lines


def _as_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()[:ITEM_CLIP]] if value.strip() else []
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            found.append(item.strip()[:ITEM_CLIP])
        elif isinstance(item, dict):
            # A source is {title, url}; a finding may be {claim, evidence}.
            text = " — ".join(
                str(item[key]).strip()
                for key in ("title", "claim", "question", "url", "evidence")
                if item.get(key)
            )
            if text:
                found.append(text[:ITEM_CLIP])
    return found


def thread_of(session: Session, run_id: UUID) -> dict[str, Any]:
    """What this task continues, and what continued it.

    Both directions, because either alone leaves a reader stranded: from the
    parent you cannot find the answer, and from the follow-up you cannot find
    the question.
    """
    run = session.get(Run, run_id)
    if run is None:
        return {"continues": None, "followed_by": []}
    continues = None
    if run.continues_run_id:
        parent = session.get(Run, run.continues_run_id)
        if parent is not None:
            continues = _brief(session, parent)
    children = session.scalars(
        select(Run).where(Run.continues_run_id == run_id).order_by(Run.started_at)
    )
    return {"continues": continues, "followed_by": [_brief(session, row) for row in children]}


def _brief(session: Session, run: Run) -> dict[str, Any]:

    task = session.get(Task, run.root_task_id)
    objective = task.objective if task else ""
    return {
        "run_id": str(run.id),
        "title": title_of(task.title if task else None, objective),
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
    }
