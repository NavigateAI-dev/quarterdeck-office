"""A thread's own settings, chosen from its composer.

Learned from DSH's input bar: the model for this conversation, its access, and
its goal sit by the box. In the office they mean:

- model: the connection the thread's replies use, over the office's choice for
  the role; only a connection that was checked and answers;
- access: whether work started here may change files in the space's folder
  (`work`, on its own branch, merging still the owner's) or only read (`read`);
  "full access" is deliberately not offered;
- goal: what the thread is for, told to the lead with every reply, and what
  work started from it may spend before it asks (over the hand-off's default).

A plan-first message asks the lead for a numbered plan instead of an answer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.budget import HANDOFF_COST
from app.db.models import ModelConnection, Topic

ACCESS = ("read", "work")
USABLE = ("MODEL_AVAILABLE", "VERIFIED")
GOAL_CHARS = 1000
CAP_RANGE = (0.05, 50.0)
PLAN_FIRST = "plan_first"
PLAN_ASK = (
    "\n\nThe owner asked for a plan first. Answer with a short numbered plan of how you "
    "would do this: the steps, what each needs, and what it may cost. Do not do the work "
    "and change nothing. If it needs work, end with the PROPOSE line as usual."
)


class SettingsRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def save(session: Session, topic: Topic, changes: dict[str, Any], writable: bool) -> None:
    """Apply what the owner changed; each field only when it was sent."""
    if "model_connection_id" in changes:
        chosen = changes["model_connection_id"]
        if chosen is not None:
            row = session.get(ModelConnection, UUID(str(chosen)))
            if row is None:
                raise SettingsRefused("connection_not_found")
            if row.status not in USABLE:
                raise SettingsRefused("connection_not_ready")
        topic.model_connection_id = chosen
    if "access" in changes:
        access = changes["access"]
        if access not in ACCESS:
            raise SettingsRefused("unknown_access")
        if access == "work" and not writable:
            raise SettingsRefused("work_access_off")
        topic.access = access
    if "goal" in changes:
        goal = " ".join(str(changes["goal"] or "").split())
        if len(goal) > GOAL_CHARS:
            raise SettingsRefused("goal_too_long")
        topic.goal = goal or None
    if "goal_cap_usd" in changes:
        cap = changes["goal_cap_usd"]
        if cap is not None and not CAP_RANGE[0] <= float(cap) <= CAP_RANGE[1]:
            raise SettingsRefused("cap_out_of_range")
        topic.goal_cap_usd = float(cap) if cap is not None else None


def view(session: Session, topic: Topic) -> dict[str, Any]:
    connection = (
        session.get(ModelConnection, topic.model_connection_id)
        if topic.model_connection_id
        else None
    )
    return {
        "model": {
            "id": str(connection.id),
            "name": connection.display_name,
            "model": connection.model_id,
            "provider": connection.provider,
        }
        if connection is not None
        else None,
        "access": topic.access or "read",
        "goal": topic.goal,
        "goal_cap_usd": topic.goal_cap_usd,
        "cap_usd": cap_of(topic),
    }


def cap_of(topic: Topic | None) -> float:
    """What work started from this thread may spend before it asks."""
    if topic is not None and topic.goal_cap_usd:
        return float(topic.goal_cap_usd)
    return HANDOFF_COST


def model_entry(session: Session, topic: Topic | None) -> dict[str, Any] | None:
    """The thread's own model as a connection entry, when it still answers."""
    if topic is None or topic.model_connection_id is None:
        return None
    row = session.get(ModelConnection, topic.model_connection_id)
    if row is None or row.status not in USABLE:
        return None
    return {
        "connection_id": str(row.id),
        "provider": row.provider,
        "model_id": row.model_id,
        "base_url": row.base_url,
    }


def goal_line(topic: Topic | None) -> str:
    """What the lead is told about the thread's goal, if it has one."""
    if topic is None or not topic.goal:
        return ""
    return f"\n\nThe owner's goal for this discussion: {topic.goal}"


def plan_first(actions: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(a, dict) and a.get("kind") == PLAN_FIRST for a in actions or [])
