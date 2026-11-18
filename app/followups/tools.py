"""The tools an agent keeps follow-ups with: the owner's own record, so they act at once."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from app.followups import service
from app.tools.pipeline import ToolResult, ToolSpec


class FollowUpUpdate(BaseModel):
    id: UUID
    status: str | None = Field(
        default=None,
        description="done, dropped, doing, open, or not_a_task for a calendar event with "
        "nothing to finish; leave empty if the owner only gave news",
    )
    note: str = Field(default="", max_length=2000, description="What the owner said, briefly")
    next_on: date | None = Field(
        default=None, description="The day to ask again, when the owner names one"
    )


class FollowUpAdd(BaseModel):
    title: str = Field(min_length=1, max_length=service.TITLE_CHARS)
    due_on: date = Field(description="The day it should be finished")


class NoArguments(BaseModel):
    pass


def follow_up_specs(
    session_factory: sessionmaker[Session],
    via: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[ToolSpec]:
    async def update(arguments: dict[str, Any]) -> ToolResult:
        asked = FollowUpUpdate.model_validate(arguments)
        with session_factory() as session, session.begin():
            try:
                row = service.record(
                    session,
                    service.found(session, asked.id),
                    clock(),
                    status=asked.status,
                    note=asked.note,
                    next_on=asked.next_on,
                    via=via,
                )
            except service.FollowUpRejected as wrong:
                return ToolResult(text=f"Not recorded: {wrong}.")
            return ToolResult(text=f"Recorded: {row.title} is {row.status}, due {row.due_on}.")

    async def add(arguments: dict[str, Any]) -> ToolResult:
        asked = FollowUpAdd.model_validate(arguments)
        with session_factory() as session, session.begin():
            row = service.add(session, asked.title, asked.due_on, clock(), via)
            return ToolResult(text=f"Following {row.title}, due {row.due_on}: {row.id}.")

    async def show(arguments: dict[str, Any]) -> ToolResult:
        with session_factory() as session:
            return ToolResult(text=service.told(session, clock()))

    return [
        ToolSpec(
            name="follow_up_update",
            description="Record where one of the owner's to-dos stands, as soon as they say so: "
            "finished, dropped, still going, or a day to ask again.",
            run=update,
            input_model=FollowUpUpdate,
        ),
        ToolSpec(
            name="follow_up_add",
            description="Follow something the owner says must be finished by a certain day: the "
            "office asks about it from that day on.",
            run=add,
            input_model=FollowUpAdd,
        ),
        ToolSpec(
            name="follow_up_list",
            description="The owner's to-dos due now, with their ids and what was last heard.",
            run=show,
            input_model=NoArguments,
            concurrency_safe=True,
        ),
    ]
