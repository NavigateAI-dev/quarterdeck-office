"""What a run is waiting on the owner for, read from its events.

A question is open from the event that asks it until one that answers it: a
limit question until a raise or the owner's "finish", a stuck step's until the
task goes on or ends. One reading, for the runner and for the thread alike;
there were two, and the thread's did not count "finish" as an answer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RunEvent

BUDGET_ASK = "budget_ask"
STUCK_ASK = "stuck_ask"
# Each question, and the events that answer it.
ANSWERED_BY = {
    BUDGET_ASK: ("budget_raised", "finish_requested"),
    STUCK_ASK: ("run_resumed", "finish_requested", "run_finished"),
}


def pending_ask(session: Session, run_id: UUID, ask: str) -> dict[str, Any] | None:
    """The question still open, as it was asked; None when never asked or answered.

    Read from the end of the log: a long run has more events than one page,
    and the question is among the last."""
    last = session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.kind.in_((ask, *ANSWERED_BY[ask])))
        .order_by(RunEvent.id.desc())
        .limit(1)
    ).first()
    if last is None or last.kind != ask:
        return None
    return dict(last.payload_json or {})
