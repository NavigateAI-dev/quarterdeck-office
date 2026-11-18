"""Writes that leave the office, checkpointed before they run.

A call to a third party's tool may change the world, and the office cannot tell
a read from a write (a server's own claim decides nothing). So each one is
written down as intended before it is sent, and settled after. A run picked up
again after a restart replays its steps; when it reaches a call it already made,
the ledger answers instead of the tool:

- **done**: made before the restart; it is not made again.
- **unknown**: the office stopped between sending it and hearing back. Whether
  it happened is not known, so it is not sent again; the owner is told, and
  settles it once they have looked.
- **failed**: it did not go through; it may be tried again.

A call is known by a digest of its run, server, tool, arguments and how many
identical calls came before it in this attempt, so two identical calls in one
run stay two. The arguments themselves are not kept: they are model output
bound for a third party (as the run's `mcp_call` events, `app/mcp/calling.py`).

The shape is the one `ChannelDelivery` (Slack) and `EngineeringExecution`
(builds) already use for theirs.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import SideEffect

INTENDED = "intended"
DONE = "done"
FAILED = "failed"
UNKNOWN = "unknown"
# What the owner may say an unknown one turned out to be.
SETTLED_BY_OWNER = (DONE, FAILED)
UNKNOWN_EVENT = "side_effect_unknown"


class SideEffectNotFound(LookupError):
    pass


class SideEffectSettled(ValueError):
    """Only an unknown outcome is the owner's to settle."""


def call_digest(
    run_id: UUID, server: str, tool: str, arguments: dict[str, Any], ordinal: int
) -> str:
    sent = json.dumps([str(run_id), server, tool, arguments, ordinal], sort_keys=True, default=str)
    return hashlib.sha256(sent.encode()).hexdigest()


class RunWrites:
    """One attempt of one run's writes: counts identical calls as it goes."""

    def __init__(
        self,
        side_effects: SideEffects,
        run_id: UUID,
        record: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.side_effects = side_effects
        self.run_id = run_id
        self.record = record
        self.seen: Counter[str] = Counter()

    def intend(
        self, kind: str, server: str, tool: str, arguments: dict[str, Any]
    ) -> tuple[int, str]:
        """Write the call down before it is sent. Returns its row and what to do:
        INTENDED (send it), DONE or UNKNOWN (do not)."""
        sameness = call_digest(self.run_id, server, tool, arguments, 0)
        self.seen[sameness] += 1
        key = call_digest(self.run_id, server, tool, arguments, self.seen[sameness])
        row_id, status = self.side_effects.intend(self.run_id, kind, key, f"{tool} on {server}")
        if status == UNKNOWN and self.record is not None:
            self.record(UNKNOWN_EVENT, {"kind": kind, "server": server, "tool": tool, "id": row_id})
        return row_id, status

    def settle(self, row_id: int, status: str, outcome: str) -> None:
        self.side_effects.settle(row_id, status, outcome)


class SideEffects:
    """The ledger: intended, then done, failed or unknown."""

    def __init__(
        self, session_factory: sessionmaker[Session], clock: Callable[[], datetime] | None = None
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock or (lambda: datetime.now(UTC))

    def for_run(
        self, run_id: UUID, record: Callable[[str, dict[str, Any]], Any] | None = None
    ) -> RunWrites:
        return RunWrites(self, run_id, record)

    def intend(self, run_id: UUID, kind: str, key: str, subject: str) -> tuple[int, str]:
        with self.session_factory() as session, session.begin():
            made = session.execute(
                insert(SideEffect)
                .values(run_id=run_id, kind=kind, key=key, subject=subject[:200], status=INTENDED)
                .on_conflict_do_nothing(index_elements=["run_id", "key"])
                .returning(SideEffect.id)
            ).scalar_one_or_none()
            if made is not None:
                return int(made), INTENDED
            row = session.scalars(
                select(SideEffect)
                .where(SideEffect.run_id == run_id, SideEffect.key == key)
                .with_for_update()
            ).one()
            if row.status == INTENDED:
                # Written down by an attempt that never heard back: the office
                # stopped mid-call. Whether it happened is not known.
                row.status = UNKNOWN
                row.outcome = "stopped_mid_call"
                row.settled_at = self.clock()
            elif row.status == FAILED:
                # It did not go through, so it may go again.
                row.status = INTENDED
                row.outcome = None
                row.settled_at = None
            return int(row.id), row.status

    def settle(self, row_id: int, status: str, outcome: str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(SideEffect, row_id)
            if row is None:
                raise SideEffectNotFound(row_id)
            row.status = status
            row.outcome = outcome[:64]
            row.settled_at = self.clock()

    def of_run(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(SideEffect).where(SideEffect.run_id == run_id).order_by(SideEffect.id)
            )
            return [view(row) for row in rows]

    def settle_by_owner(self, row_id: int, status: str, owner: str) -> dict[str, Any]:
        """The owner looked at the third party and says what happened."""
        if status not in SETTLED_BY_OWNER:
            raise ValueError(status)
        with self.session_factory() as session, session.begin():
            row = session.get(SideEffect, row_id)
            if row is None:
                raise SideEffectNotFound(row_id)
            if row.status != UNKNOWN:
                raise SideEffectSettled(row.status)
            row.status = status
            row.outcome = "settled_by_owner"
            row.settled_by = owner[:128]
            row.settled_at = self.clock()
            session.flush()
            return view(row)


def view(row: SideEffect) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": str(row.run_id),
        "kind": row.kind,
        "subject": row.subject,
        "status": row.status,
        "outcome": row.outcome,
        "settled_by": row.settled_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "settled_at": row.settled_at.isoformat() if row.settled_at else None,
    }
