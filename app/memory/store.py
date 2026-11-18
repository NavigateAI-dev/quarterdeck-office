"""Project memory: recording candidates, approving them, and reading them back.

The loop is deliberately review-gated, as Beacon's is. An evaluation records
candidates and, when asked, what Jev thought of them. Nothing reaches an agent
until the owner approves it, and a probability never approves anything.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Project, ProjectMemory
from app.memory.candidates import Candidate

# Where a kept lesson applies. The owner chooses when they keep it: what one
# project learned about its own work is not what a whole business line knows.
PROJECT = "project"
VENTURE = "venture"
OFFICE = "office"
SCOPES = (PROJECT, VENTURE, OFFICE)

CANDIDATE = "CANDIDATE"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
# What reaches a run. More than this and the context stops being a reminder.
RECALL_LIMIT = 12


def digest_of(lesson: str) -> str:
    return hashlib.sha256(lesson.strip().lower().encode()).hexdigest()


def record(
    session: Session,
    project_id: UUID,
    run_id: UUID | None,
    candidates: list[Candidate],
    probabilities: list[float] | None = None,
    evaluator: str | None = None,
) -> int:
    """Store candidates for review. A lesson already known is left as it is.

    Returns how many were new: re-evaluating a run does not multiply its
    lessons, and does not undo a decision the owner already made about one.
    """
    scored = probabilities or []
    now = datetime.now(UTC)
    added = 0
    for index, candidate in enumerate(candidates):
        values: dict[str, Any] = {
            "project_id": project_id,
            "run_id": run_id,
            "kind": candidate.kind,
            "lesson": candidate.lesson,
            "lesson_digest": digest_of(candidate.lesson),
            "evidence": candidate.evidence,
            "status": CANDIDATE,
        }
        if index < len(scored):
            values |= {
                "reusable_probability": scored[index],
                "evaluator": evaluator,
                "evaluated_at": now,
            }
        statement = (
            insert(ProjectMemory)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["project_id", "lesson_digest"])
            .returning(ProjectMemory.id)
        )
        if session.execute(statement).scalar_one_or_none() is not None:
            added += 1
    return added


def decide(
    session: Session, memory_id: UUID, approved: bool, owner: str, scope: str = PROJECT
) -> bool:
    """Approve or reject one candidate. Only the owner reaches this.

    A lesson kept for a venture records that venture, so recall needs no join
    and moving a project later cannot silently change what earlier work knows.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope}")
    row = session.get(ProjectMemory, memory_id)
    if row is None or row.status != CANDIDATE:
        return False
    venture_id = None
    if scope == VENTURE:
        project = session.get(Project, row.project_id)
        venture_id = project.venture_id if project is not None else None
        if venture_id is None:
            # Nothing to keep it for: a project outside every business line.
            scope = PROJECT
    row.status = APPROVED if approved else REJECTED
    row.scope = scope
    row.venture_id = venture_id
    row.decided_at = datetime.now(UTC)
    row.decided_by = owner
    session.flush()
    return True


def candidates(session: Session, project_id: UUID) -> list[dict[str, Any]]:
    """What is waiting for the owner, most likely to be useful first."""
    rows = session.scalars(
        select(ProjectMemory)
        .where(ProjectMemory.project_id == project_id, ProjectMemory.status == CANDIDATE)
        .order_by(ProjectMemory.reusable_probability.desc().nullslast(), ProjectMemory.created_at)
    )
    return [view(row) for row in rows]


def approved(session: Session, project_id: UUID, limit: int = RECALL_LIMIT) -> list[str]:
    """The lessons an agent should be told, newest decision first.

    What this project learned, what its business line learned, and what the
    office knows everywhere. A project belonging to no venture sees only its
    own and the office's, exactly as before ventures existed.
    """
    project = session.get(Project, project_id)
    venture_id = project.venture_id if project is not None else None
    applies = [
        (ProjectMemory.scope == PROJECT) & (ProjectMemory.project_id == project_id),
        ProjectMemory.scope == OFFICE,
    ]
    if venture_id is not None:
        applies.append((ProjectMemory.scope == VENTURE) & (ProjectMemory.venture_id == venture_id))
    rows = session.scalars(
        select(ProjectMemory)
        .where(ProjectMemory.status == APPROVED, or_(*applies))
        .order_by(ProjectMemory.decided_at.desc().nullslast())
        .limit(limit)
    )
    return [row.lesson for row in rows]


def view(row: ProjectMemory) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "run_id": str(row.run_id) if row.run_id else None,
        "kind": row.kind,
        "lesson": row.lesson,
        "evidence": row.evidence,
        "status": row.status,
        "scope": row.scope,
        "reusable_probability": row.reusable_probability,
        "evaluator": row.evaluator,
        "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }
