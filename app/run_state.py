"""A run's state as a projection of its log of changes.

Every change to a run's state is appended to `run_state_changes` (set these
keys, or replace the whole) and `runs.state_json` is updated from it, in the
same transaction. So the state is what its changes add up to, and can be
rebuilt from them alone (`rebuild`); `state_json` stays as the quick read the
rest of the office already uses.

This module is the one place that writes a run's state; an architecture test
(`tests/architecture/test_run_state_writes.py`) keeps it that way. The changes
live apart from `run_events` (progress and asks, what the page shows), because
a run's state can be large, and the feed, the flow map and the Desk read the
events often.

LangGraph checkpoints stay as a resume cache for team runs.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Run, RunStateChange

SET = "set"
REPLACE = "replace"
OPS = (SET, REPLACE)


def clean(values: dict[str, Any]) -> dict[str, Any]:
    """As JSON would keep it: what goes in the log is what comes back out."""
    return dict(json.loads(json.dumps(dict(values), default=str)))


def project(state: dict[str, Any] | None, op: str, patch: dict[str, Any]) -> dict[str, Any]:
    """The state after one change."""
    if op == REPLACE:
        return dict(patch)
    if op == SET:
        return {**(state or {}), **patch}
    raise ValueError(f"unknown state change: {op}")


def write(
    session: Session, run: Run, op: str, values: dict[str, Any], source: str
) -> dict[str, Any]:
    """Append one change to the run's log and project it onto the run, in the
    caller's transaction. Returns the state after it."""
    patch = clean(values)
    session.add(RunStateChange(run_id=run.id, op=op, patch_json=patch, source=source[:32]))
    run.state_json = project(run.state_json, op, patch)
    return dict(run.state_json)


def rebuild(session: Session, run_id: UUID) -> dict[str, Any]:
    """The run's state from its log alone."""
    state: dict[str, Any] = {}
    for change in session.scalars(
        select(RunStateChange).where(RunStateChange.run_id == run_id).order_by(RunStateChange.id)
    ):
        state = project(state, change.op, dict(change.patch_json or {}))
    return state


def changes(session: Session, run_id: UUID) -> list[dict[str, Any]]:
    """The log as it reads: each change, which keys it touched, and who made it."""
    rows = session.scalars(
        select(RunStateChange).where(RunStateChange.run_id == run_id).order_by(RunStateChange.id)
    )
    return [
        {
            "id": row.id,
            "op": row.op,
            "keys": sorted((row.patch_json or {}).keys()),
            "source": row.source,
            "at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
