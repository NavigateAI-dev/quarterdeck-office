"""What a business line is trying to achieve, and whether work moved it.

An objective is the reason a task exists beyond itself. The roll-up is
deliberately unflattering: a task that finished without answering what it was
asked counts as no progress, because counting it would make a quarter look
worked rather than advanced.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifacts import ArtifactStore
from app.db.models import Artifact, Objective, Run

OPEN = "OPEN"
# Work that reached an end, one way or another.
FINISHED = ("COMPLETED", "NEEDS_HUMAN_REVIEW", "BLOCKED", "REJECTED")
DELIVERED = "delivered"
PARTIAL = "partial"
UNFINISHED = "unfinished"


def active(session: Session, venture_id: UUID | None, today: date | None = None) -> list[Objective]:
    """Open objectives whose window has not closed."""
    if venture_id is None:
        return []
    now = today or datetime.now(UTC).date()
    rows = session.scalars(
        select(Objective)
        .where(Objective.venture_id == venture_id, Objective.status == OPEN)
        .order_by(Objective.ends_on.asc().nullslast(), Objective.created_at)
    )
    return [row for row in rows if row.ends_on is None or row.ends_on >= now]


def as_lines(objectives: list[Objective]) -> list[str]:
    """Objectives as the short lines an agent is told."""
    lines = []
    for row in objectives:
        line = row.title
        if row.measure:
            line += f" — measured by: {row.measure}"
        if row.ends_on:
            line += f" (by {row.ends_on.isoformat()})"
        lines.append(line)
    return lines


def _verdict(run: Run, coverage: dict[str, Any] | None) -> str:
    """Whether this run moved its objective, on its own account of itself."""
    if run.status not in FINISHED:
        return UNFINISHED
    if coverage is None:
        # No scorecard: a completed run is taken at its word, anything else
        # is not.
        return DELIVERED if run.status == "COMPLETED" else PARTIAL
    asked = int(coverage.get("questions_asked") or 0)
    answered = int(coverage.get("questions_answered") or 0)
    unmet = list(coverage.get("unmet_by_review") or [])
    if asked and answered == 0:
        # It finished and answered nothing it was asked. That is not progress.
        return UNFINISHED
    if unmet or (asked and answered < asked):
        return PARTIAL
    return DELIVERED if run.status == "COMPLETED" else PARTIAL


def _coverage_of(session: Session, run: Run, store: ArtifactStore) -> dict[str, Any] | None:
    artifact = session.scalars(
        select(Artifact)
        .where(Artifact.run_id == run.id, Artifact.type == "final_report")
        .order_by(Artifact.version.desc())
        .limit(1)
    ).first()
    if artifact is None:
        return None
    try:
        payload = json.loads(store.read(artifact.project_id, artifact.id, artifact.version))
    except (OSError, ValueError):
        return None
    found = payload.get("coverage") if isinstance(payload, dict) else None
    return found if isinstance(found, dict) else None


def progress(session: Session, objective: Objective, store: ArtifactStore) -> dict[str, Any]:
    """What the work serving this objective actually came to."""
    runs = list(
        session.scalars(
            select(Run).where(Run.objective_id == objective.id).order_by(Run.started_at)
        )
    )
    counted = {DELIVERED: 0, PARTIAL: 0, UNFINISHED: 0}
    work = []
    for run in runs:
        verdict = _verdict(run, _coverage_of(session, run, store))
        counted[verdict] += 1
        work.append({"run_id": str(run.id), "status": run.status, "verdict": verdict})
    return {
        "id": str(objective.id),
        "title": objective.title,
        "measure": objective.measure,
        "starts_on": objective.starts_on.isoformat() if objective.starts_on else None,
        "ends_on": objective.ends_on.isoformat() if objective.ends_on else None,
        "status": objective.status,
        "tasks": len(runs),
        **counted,
        "work": work,
    }
