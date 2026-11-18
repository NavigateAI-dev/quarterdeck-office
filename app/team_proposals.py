"""The lead's proposal of a full team run, and the task it became.

The lead cannot write code or run the whole pipeline itself; when a request
needs that, it proposes a team run (`propose_team_run`) and answers with what
it has. The proposal is kept as the lead task's `team_run_proposed` event; the
owner's press on that task starts it once, as a full team task that builds on
the lead's, and `team_run_started` names the task it became.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RunEvent

PROPOSED = "team_run_proposed"
STARTED = "team_run_started"


def of_run(session: Session, run_id: UUID) -> dict[str, Any] | None:
    """The lead's latest proposal on this task, and the task it started, if any."""
    proposed = _latest(session, run_id, PROPOSED)
    if proposed is None:
        return None
    started = _latest(session, run_id, STARTED)
    return {
        "objective": str(proposed.get("objective") or ""),
        "why": str(proposed.get("why") or ""),
        "started_run_id": started.get("run_id") if started else None,
    }


def started(session: Session, run_id: UUID, team_run_id: UUID) -> None:
    session.add(RunEvent(run_id=run_id, kind=STARTED, payload_json={"run_id": str(team_run_id)}))


def _latest(session: Session, run_id: UUID, kind: str) -> dict[str, Any] | None:
    payload = session.scalars(
        select(RunEvent.payload_json)
        .where(RunEvent.run_id == run_id, RunEvent.kind == kind)
        .order_by(RunEvent.id.desc())
        .limit(1)
    ).first()
    return dict(payload) if payload is not None else None
