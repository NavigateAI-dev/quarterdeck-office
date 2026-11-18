"""The owner's verdict on the lead's answers, and the numbers that judge it
(decision record 0007).

Quality is judged on real use, not by a paid judge: a 👍 or 👎 on each answer
(a discussion reply, or a task's answer), "redo with the full team" when the
lead fell short, and what each request cost in calls, time and money, lead
beside team. Everything here is read from what the office already records;
nothing calls a model.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AnswerVerdict, RoomMessage, Run, UsageEvent

VERDICTS = ("up", "down")
NOTE_CHARS = 1000
LEAD = "lead"
# A reply that flags citations no tool returned (room_replies) says this.
UNCONFIRMED = "_Not confirmed by a tool this time:"


class VerdictRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _checked(verdict: str, note: str | None) -> str | None:
    if verdict not in VERDICTS:
        raise VerdictRefused("unknown_verdict")
    said = " ".join(str(note or "").split())
    if len(said) > NOTE_CHARS:
        raise VerdictRefused("note_too_long")
    return said or None


def judge_reply(
    session: Session, topic_id: UUID, message_id: int, verdict: str, note: str | None
) -> AnswerVerdict:
    """The owner's verdict on an agent's reply in this discussion; the latest wins."""
    said = _checked(verdict, note)
    message = session.get(RoomMessage, message_id)
    if message is None or message.topic_id != topic_id:
        raise VerdictRefused("message_not_found")
    if message.author_kind != "agent":
        raise VerdictRefused("not_an_answer")
    row = session.scalar(select(AnswerVerdict).where(AnswerVerdict.room_message_id == message_id))
    if row is None:
        row = AnswerVerdict(room_message_id=message_id, verdict=verdict)
        session.add(row)
    row.verdict, row.note = verdict, said
    session.flush()
    return row


def judge_run(session: Session, run_id: UUID, verdict: str, note: str | None) -> AnswerVerdict:
    """The owner's verdict on a task's answer; the latest wins."""
    said = _checked(verdict, note)
    run = session.get(Run, run_id)
    if run is None:
        raise VerdictRefused("run_not_found")
    if run.status != "COMPLETED":
        raise VerdictRefused("not_answered_yet")
    row = session.scalar(
        select(AnswerVerdict).where(
            AnswerVerdict.run_id == run_id, AnswerVerdict.room_message_id.is_(None)
        )
    )
    if row is None:
        row = AnswerVerdict(run_id=run_id, verdict=verdict)
        session.add(row)
    row.verdict, row.note = verdict, said
    session.flush()
    return row


def question_of(session: Session, message_id: int) -> str | None:
    """What the owner asked that this reply answered: the message it replies to."""
    message = session.get(RoomMessage, message_id)
    if message is None or message.reply_to is None:
        return None
    asked = session.get(RoomMessage, message.reply_to)
    if asked is None or asked.author_kind != "owner":
        return None
    return asked.body.strip() or None


def view(row: AnswerVerdict | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "verdict": row.verdict,
        "note": row.note,
        "redo_run_id": str(row.redo_run_id) if row.redo_run_id else None,
    }


def of_messages(session: Session, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Verdicts on these messages, for the feed."""
    if not ids:
        return {}
    return {
        int(row.room_message_id): view(row) or {}
        for row in session.scalars(
            select(AnswerVerdict).where(AnswerVerdict.room_message_id.in_(ids))
        )
        if row.room_message_id is not None
    }


def of_run(session: Session, run_id: UUID) -> dict[str, Any] | None:
    return view(
        session.scalar(
            select(AnswerVerdict).where(
                AnswerVerdict.run_id == run_id, AnswerVerdict.room_message_id.is_(None)
            )
        )
    )


def _median(values: list[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def report(session: Session, now: datetime, days: int = 30) -> dict[str, Any]:
    """Lead beside team over `days`: per-request calls, time and cost, outcomes,
    the owner's verdicts and reworks within a day, and replies."""
    since = now - timedelta(days=days)
    runs = list(
        session.execute(
            select(Run.id, Run.workflow_name, Run.status, Run.started_at, Run.completed_at).where(
                Run.started_at >= since
            )
        )
    )
    usage: dict[UUID, tuple[int, float]] = {
        run_id: (int(calls or 0), float(cost or 0.0))
        for run_id, calls, cost in session.execute(
            select(
                UsageEvent.run_id,
                func.count().filter(UsageEvent.role != "tools"),
                func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0),
            )
            .where(UsageEvent.run_id.in_([row[0] for row in runs] or [None]))
            .group_by(UsageEvent.run_id)
        )
    }
    paths: dict[str, dict[str, Any]] = {}
    for run_id, workflow, status, started, completed in runs:
        path = LEAD if workflow == LEAD else "team"
        entry = paths.setdefault(
            path, {"runs": 0, "completed": 0, "failed": 0, "calls": [], "secs": [], "cost": []}
        )
        entry["runs"] += 1
        entry["completed"] += status == "COMPLETED"
        entry["failed"] += status == "FAILED"
        calls, cost = usage.get(run_id, (0, 0.0))
        entry["calls"].append(calls)
        entry["cost"].append(cost)
        if started and completed:
            entry["secs"].append((completed - started).total_seconds())
    shown = {
        path: {
            "runs": entry["runs"],
            "completed": entry["completed"],
            "failed": entry["failed"],
            "median_calls": _median(entry["calls"]),
            "median_seconds": _median(entry["secs"]),
            "median_cost_usd": _median(entry["cost"]),
            "total_cost_usd": round(sum(entry["cost"]), 4),
        }
        for path, entry in paths.items()
    }
    replies = list(
        session.execute(
            select(RoomMessage.tokens, RoomMessage.cost_usd, RoomMessage.body).where(
                RoomMessage.author_kind == "agent", RoomMessage.created_at >= since
            )
        )
    )
    verdicts = list(
        session.execute(
            select(
                AnswerVerdict.verdict, AnswerVerdict.redo_run_id, AnswerVerdict.room_message_id
            ).where(AnswerVerdict.updated_at >= since)
        )
    )
    first = Run.__table__.alias("first")
    reworked = session.scalar(
        select(func.count())
        .select_from(Run)
        .join(first, Run.continues_run_id == first.c.id)
        .where(
            first.c.completed_at >= since,
            Run.started_at < first.c.completed_at + timedelta(hours=24),
        )
    )
    return {
        "days": days,
        "paths": shown,
        "replies": {
            "count": len(replies),
            "median_tokens": _median([float(t or 0) for t, _, _ in replies]),
            "total_cost_usd": round(sum(float(c or 0.0) for _, c, _ in replies), 4),
            "unconfirmed_citations": sum(1 for _, _, body in replies if UNCONFIRMED in body),
        },
        "verdicts": {
            "up": sum(1 for v, _, _ in verdicts if v == "up"),
            "down": sum(1 for v, _, _ in verdicts if v == "down"),
            "redone_with_team": sum(1 for _, redo, _ in verdicts if redo),
            "on_replies": sum(1 for _, _, message in verdicts if message is not None),
        },
        "reworked_within_a_day": int(reworked or 0),
    }
