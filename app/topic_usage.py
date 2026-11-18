"""A thread's numbers for its status line, and the thread as a download.

Learned from DSH's line under the box (turns, steps, tokens, cache, context):
what the office knows of a thread is its messages, what each reply cost, and
the work started from it. Cache hits are not recorded for model calls, so they
are not shown. Context use is the last reply's tokens against its model's
published window, and only when that window is known.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import RoomMessage, Run, Task, Topic, UsageEvent
from app.model_catalog import cached_prices, describe
from app.office_settings import role_models
from app.topic_settings import model_entry

STEP_ROLES_EXCLUDED = ("tools",)


def usage(session: Session, settings: Settings, topic: Topic) -> dict[str, Any]:
    """Turns, replies, the work's steps, tokens and cost, and context used."""
    turns, replies, reply_tokens, reply_cost = 0, 0, 0, 0.0
    last_reply_tokens: int | None = None
    for kind, tokens, cost in session.execute(
        select(RoomMessage.author_kind, RoomMessage.tokens, RoomMessage.cost_usd)
        .where(RoomMessage.topic_id == topic.id)
        .order_by(RoomMessage.id)
    ):
        if kind == "owner":
            turns += 1
        elif kind == "agent":
            replies += 1
            reply_tokens += int(tokens or 0)
            reply_cost += float(cost or 0.0)
            last_reply_tokens = int(tokens) if tokens else last_reply_tokens
    runs = list(session.scalars(select(Run.id).where(Run.topic_id == topic.id)))
    steps, work_tokens, work_cost = 0, 0, 0.0
    if runs:
        steps, work_tokens, work_cost = session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(
                    func.sum(
                        func.coalesce(UsageEvent.prompt_tokens, 0)
                        + func.coalesce(UsageEvent.completion_tokens, 0)
                    ),
                    0,
                ),
                func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0),
            ).where(UsageEvent.run_id.in_(runs), UsageEvent.role.not_in(STEP_ROLES_EXCLUDED))
        ).one()
    window = _window(session, settings, topic)
    return {
        "turns": turns,
        "replies": replies,
        "work": len(runs),
        "steps": int(steps or 0),
        "tokens": reply_tokens + int(work_tokens or 0),
        "cost_usd": round(reply_cost + float(work_cost or 0.0), 4),
        "context": {
            "used": last_reply_tokens,
            "window": window,
            "percent": round(100 * last_reply_tokens / window)
            if window and last_reply_tokens
            else None,
        },
    }


def _window(session: Session, settings: Settings, topic: Topic) -> int | None:
    """The context window of the model the thread's replies use, when published."""
    entry = model_entry(session, topic) or role_models(session, settings).get("chief")
    if entry is None:
        return None
    described = describe(
        cached_prices(settings.workspace_root), str(entry["provider"]), str(entry["model_id"])
    )
    window = described.get("context_window")
    return int(window) if isinstance(window, int | float) and window > 0 else None


def export(session: Session, topic: Topic) -> tuple[str, bytes]:
    """The thread as a zip: its messages in Markdown, the work it started, and
    the files attached to it by name. Nothing secret is in a thread's words."""
    lines = [f"# {topic.title}", ""]
    if topic.goal:
        lines += [f"**Goal:** {topic.goal}", ""]
    attached: list[str] = []
    for message in session.scalars(
        select(RoomMessage).where(RoomMessage.topic_id == topic.id).order_by(RoomMessage.id)
    ):
        when = _when(message.created_at)
        lines += [f"## {message.author_name} · {when}", "", message.body.strip(), ""]
        for action in message.actions_json or []:
            if isinstance(action, dict) and action.get("kind") == "attached":
                attached.append(str(action.get("name") or ""))
    work = ["# Work started from this thread", ""]
    for run, objective in session.execute(
        select(Run, Task.objective)
        .join(Task, Task.id == Run.root_task_id)
        .where(Run.topic_id == topic.id)
        .order_by(Task.created_at)
    ):
        work.append(f"- {objective.strip()[:300]} — {run.status} (task {run.id})")
    if len(work) == 2:
        work.append("Nothing was started from this thread.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("thread.md", "\n".join(lines).rstrip() + "\n")
        archive.writestr("work.md", "\n".join(work) + "\n")
        archive.writestr(
            "attachments.txt",
            "\n".join(name for name in attached if name) + "\n" if attached else "None.\n",
        )
    return f"{_slug(topic.title)}.zip", buffer.getvalue()


def _when(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC") if at else ""


def _slug(title: str) -> str:
    kept = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
    return Path("-".join(part for part in kept.split("-") if part)[:60]).name or "thread"
