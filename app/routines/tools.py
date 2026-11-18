"""The tools an agent manages routines with, in a discussion.

As an agent's outside tools that ask first: creating or changing one only
proposes it, as a button under the reply. Stopping one narrows what the office
does, so it happens at once and is named on the reply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from app.routines import service
from app.tools.pipeline import ToolResult, ToolSpec


class Timing(BaseModel):
    frequency: str | None = Field(
        default=None, description="daily, weekly, hours, cron or once; leave empty to keep it"
    )
    local_time: str | None = Field(default=None, description="HH:MM, for daily or weekly")
    weekday: int | None = Field(default=None, description="Monday 0 to Sunday 6, for weekly")
    every_hours: int | None = Field(default=None, description="For hours")
    cron: str | None = Field(default=None, description="Five fields, for cron")
    once_at: datetime | None = Field(default=None, description="With its zone, for once")


class RoutineCreate(Timing):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(
        min_length=1,
        max_length=4000,
        description="What to do each time, with everything the owner said they want from it",
    )
    quiet_when: str = Field(
        default="", description="When to send nothing, in the owner's words; empty to always send"
    )


class RoutineUpdate(Timing):
    id: UUID
    title: str | None = None
    prompt: str | None = Field(default=None, description="The whole new prompt, not a change")
    quiet_when: str | None = None


class RoutineStop(BaseModel):
    id: UUID


class NoArguments(BaseModel):
    pass


def routine_specs(
    session_factory: sessionmaker[Session],
    topic_id: UUID,
    agent_profile_id: str | None,
    proposed: list[dict[str, Any]],
) -> list[ToolSpec]:
    async def create(arguments: dict[str, Any]) -> ToolResult:
        asked = RoutineCreate.model_validate(arguments)
        with session_factory() as session:
            zone = service.owner_timezone(session)
        try:
            rule = service.rule_from(asked.model_dump(), zone)
        except service.RoutineRejected as wrong:
            return ToolResult(text=f"Not proposed: {wrong}.")
        proposed.append(
            {
                "kind": "routine_create",
                "title": asked.title,
                "prompt": asked.prompt,
                "quiet_when": asked.quiet_when,
                "when": service.when(rule),
                "rule": rule.model_dump(mode="json"),
                "agent_profile_id": agent_profile_id,
            }
        )
        return ToolResult(
            text="Proposed to the owner as a button under your reply; it is not set until "
            "they press it. Never claim it is done."
        )

    async def update(arguments: dict[str, Any]) -> ToolResult:
        asked = RoutineUpdate.model_validate(arguments)
        with session_factory() as session:
            try:
                row = service.found(session, asked.id)
            except service.RoutineRejected as wrong:
                return ToolResult(text=f"Not proposed: {wrong}.")
            title = row.title
        changes = asked.model_dump(mode="json", exclude_none=True)
        changes["id"] = str(asked.id)
        proposed.append({"kind": "routine_update", "title": title, "changes": changes})
        return ToolResult(
            text="Proposed to the owner as a button under your reply; nothing has changed "
            "until they press it. Never claim it is done."
        )

    async def stop(arguments: dict[str, Any]) -> ToolResult:
        asked = RoutineStop.model_validate(arguments)
        with session_factory() as session, session.begin():
            try:
                row = service.found(session, asked.id)
            except service.RoutineRejected as wrong:
                return ToolResult(text=f"Not stopped: {wrong}.")
            service.stop(session, row)
            title = row.title
        proposed.append({"kind": "routine_stopped", "title": title})
        return ToolResult(text=f"{title} is stopped. The owner can turn it back on under Routines.")

    async def show(arguments: dict[str, Any]) -> ToolResult:
        with session_factory() as session:
            return ToolResult(text=_shown(session))

    return [
        ToolSpec(
            name="routine_create",
            description="Propose recurring work, or one job at a later time, that the office "
            "does on its own and reports in this discussion. Use it when the owner asks for "
            "something every day, every week or later on. They press to set it.",
            run=create,
            input_model=RoutineCreate,
        ),
        ToolSpec(
            name="routine_update",
            description="Propose a change to a routine: its timing, or what goes in it when the "
            "owner says they want something different. They press to apply it.",
            run=update,
            input_model=RoutineUpdate,
        ),
        ToolSpec(
            name="routine_stop",
            description="Stop a routine at once, when the owner asks.",
            run=stop,
            input_model=RoutineStop,
        ),
        ToolSpec(
            name="routine_list",
            description="The routines that exist, with their ids, timing and words.",
            run=show,
            input_model=NoArguments,
            concurrency_safe=True,
        ),
    ]


def _shown(session: Session) -> str:
    rows = service.listed(session)
    if not rows:
        return "No routine is set."
    return "\n\n".join(
        f"{row.id}: {row.title}, {service.when(service.rule_of(row))}"
        f"{'' if row.enabled else ' (off)'}\n{row.prompt}"
        for row in rows
    )


def press(session: Session, action: dict[str, Any], topic_id: UUID) -> dict[str, Any]:
    """The owner's press on a proposal: it is set. What to record on the button."""
    now = datetime.now(UTC)
    if action.get("kind") == "routine_create":
        rule = service.rule_from(dict(action["rule"]), str(action["rule"]["timezone"]))
        agent = action.get("agent_profile_id")
        row = service.create(
            session,
            title=str(action.get("title")),
            prompt=str(action.get("prompt")),
            quiet_when=str(action.get("quiet_when") or ""),
            rule=rule,
            now=now,
            topic_id=topic_id,
            agent_profile_id=UUID(str(agent)) if agent else None,
        )
        return {"routine_id": str(row.id)}
    changes = dict(action.get("changes") or {})
    row = service.found(session, UUID(str(changes.pop("id"))))
    service.update(session, row, changes, now)
    return {"routine_id": str(row.id)}
