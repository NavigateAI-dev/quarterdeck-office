"""Recording what the office learns about a subject, and telling it back."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Entity, EntityFact, Project
from app.entities import DOSSIER_LIMIT, FactCandidate, about, digest_of

CANDIDATE = "CANDIDATE"
APPROVED = "APPROVED"
REJECTED = "REJECTED"


def known(session: Session, venture_id: UUID | None) -> list[Entity]:
    """The subjects this business line knows, plus the office's own."""
    statement = select(Entity).where(Entity.status == "ACTIVE")
    rows = list(session.scalars(statement))
    return [row for row in rows if row.venture_id in (None, venture_id)]


def record(
    session: Session,
    entities: list[Entity],
    candidates: list[FactCandidate],
    run_id: UUID | None,
    probabilities: list[float] | None = None,
    evaluator: str | None = None,
) -> int:
    """Store what a run said about each subject, for the owner to review."""
    by_name = {str(row.name): row for row in entities}
    scored = probabilities or []
    added = 0
    for index, candidate in enumerate(candidates):
        entity = by_name.get(candidate.entity)
        if entity is None:
            continue
        values: dict[str, Any] = {
            "entity_id": entity.id,
            "run_id": run_id,
            "fact": candidate.fact,
            "fact_digest": digest_of(candidate.fact),
            "evidence": candidate.evidence,
            "status": CANDIDATE,
        }
        if index < len(scored):
            values["durable_probability"] = scored[index]
            values["evaluator"] = evaluator
        statement = (
            insert(EntityFact)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["entity_id", "fact_digest"])
            .returning(EntityFact.id)
        )
        if session.execute(statement).scalar_one_or_none() is not None:
            added += 1
    return added


def decide(session: Session, fact_id: UUID, approved: bool, owner: str) -> bool:
    changed = session.execute(
        update(EntityFact)
        .where(EntityFact.id == fact_id, EntityFact.status == CANDIDATE)
        .values(
            status=APPROVED if approved else REJECTED,
            decided_at=datetime.now(UTC),
            decided_by=owner,
        )
        .returning(EntityFact.id)
    ).first()
    return changed is not None


def dossiers_for(session: Session, project: Project, objective: str) -> list[str]:
    """The dossiers of the subjects this task is about, and no others.

    A task about the Wwft is not handed everything the office knows about every
    brand it has researched.
    """
    entities = known(session, project.venture_id)
    wanted = about(objective, entities)
    if not wanted:
        return []
    rows = session.scalars(
        select(EntityFact)
        .where(
            EntityFact.entity_id.in_([row.id for row in wanted]),
            EntityFact.status == APPROVED,
        )
        .order_by(EntityFact.decided_at.desc().nullslast())
        .limit(DOSSIER_LIMIT)
    )
    names = {row.id: row.name for row in wanted}
    return [f"{names.get(row.entity_id, 'Subject')}: {row.fact}" for row in rows]


def candidates(session: Session, entity_id: UUID) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(EntityFact)
        .where(EntityFact.entity_id == entity_id, EntityFact.status == CANDIDATE)
        .order_by(EntityFact.durable_probability.desc().nullslast(), EntityFact.created_at)
    )
    return [view(row) for row in rows]


def view(row: EntityFact) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "entity_id": str(row.entity_id),
        "run_id": str(row.run_id) if row.run_id else None,
        "fact": row.fact,
        "evidence": row.evidence,
        "status": row.status,
        "durable_probability": row.durable_probability,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }
