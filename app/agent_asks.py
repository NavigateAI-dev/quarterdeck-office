"""A coding agent asking the owner's permission mid-task.

An Agent Client Protocol agent may stop and ask before it does something (run a
command, write outside its plan). The office never answers for the owner: the
question is put on the run, with the agent's own choices as buttons, and the
run waits for the owner's answer. Unanswered after ten minutes
it is refused, and the agent carries on or stops on its own.

The question and its outcome are the run's events (`agent_permission_ask`,
`agent_permission_settled`), so the thread shows it and the record keeps it; the
owner's answer travels as a run control (`permission`), as their other words to
a running task do.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Run, RunControl, RunEvent, Task
from app.titles import title_of

ASK = "agent_permission_ask"
SETTLED = "agent_permission_settled"
CONTROL = "permission"
WAIT = timedelta(minutes=10)
POLL_SECONDS = 1.0


class AskRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentAsks:
    """Puts an agent's question on its run and waits for the owner."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        record: Callable[[UUID, str, dict[str, Any]], Any],
        wait: timedelta = WAIT,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.record = record
        self.wait = wait
        self.poll_seconds = poll_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    def for_run(
        self, run_id: UUID, agent: str
    ) -> Callable[[str, list[dict[str, str]]], Awaitable[str | None]]:
        async def ask(title: str, options: list[dict[str, str]]) -> str | None:
            return await self.ask(run_id, agent, title, options)

        return ask

    async def ask(
        self, run_id: UUID, agent: str, title: str, options: list[dict[str, str]]
    ) -> str | None:
        """The owner's chosen option id, or None when unanswered by the deadline."""
        ask_id = uuid4().hex
        deadline = self.clock() + self.wait
        shown = [
            {
                "id": str(o.get("id") or ""),
                "name": str(o.get("name") or ""),
                "kind": str(o.get("kind") or ""),
            }
            for o in options
            if o.get("id")
        ]
        self.record(
            run_id,
            ASK,
            {
                "ask_id": ask_id,
                "agent": agent,
                "title": title[:300],
                "options": shown,
                "expires_at": deadline.isoformat(),
            },
        )
        chosen: str | None = None
        while self.clock() < deadline:
            chosen = await asyncio.to_thread(self._take, run_id, ask_id, {o["id"] for o in shown})
            if chosen is not None:
                break
            await asyncio.sleep(self.poll_seconds)
        self.record(
            run_id,
            SETTLED,
            {
                "ask_id": ask_id,
                "option": chosen,
                "outcome": "answered" if chosen else "refused_unanswered",
            },
        )
        return chosen

    def _take(self, run_id: UUID, ask_id: str, allowed: set[str]) -> str | None:
        with self.session_factory() as session, session.begin():
            row = session.scalars(
                select(RunControl)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind == CONTROL,
                    RunControl.target_node == ask_id,
                    RunControl.status == "QUEUED",
                )
                .order_by(RunControl.created_at)
                .limit(1)
            ).first()
            if row is None:
                return None
            row.status = "APPLIED"
            row.applied_at = self.clock()
            option = str(json.loads(row.text or "{}").get("option") or "")
            return option if option in allowed else None

    def answer(self, run_id: UUID, ask_id: str, option: str) -> None:
        """The owner's choice, for a question still open."""
        with self.session_factory() as session, session.begin():
            asked = open_asks(session, Run.id == run_id, self.clock())
            question = next((q for q in asked if q["ask_id"] == ask_id), None)
            if question is None:
                raise AskRefused("ask_not_open")
            if option not in {o["id"] for o in question["options"]}:
                raise AskRefused("option_unknown")
            session.add(
                RunControl(
                    run_id=run_id,
                    kind=CONTROL,
                    text=json.dumps({"option": option}),
                    status="QUEUED",
                    target_node=ask_id,
                )
            )


def open_asks(session: Session, which: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """Agents' questions still waiting for the owner, for the runs `which` selects."""
    moment = now or datetime.now(UTC)
    rows = session.execute(
        select(RunEvent, Task.objective, Task.title)
        .join(Run, Run.id == RunEvent.run_id)
        .join(Task, Task.id == Run.root_task_id)
        .where(which, RunEvent.kind.in_((ASK, SETTLED)), RunEvent.created_at >= moment - WAIT * 2)
        .order_by(RunEvent.id)
    ).all()
    asked: dict[str, dict[str, Any]] = {}
    for event, objective, title in rows:
        payload = dict(event.payload_json or {})
        ask_id = str(payload.get("ask_id") or "")
        if event.kind == SETTLED:
            asked.pop(ask_id, None)
            continue
        expires = payload.get("expires_at")
        if expires and datetime.fromisoformat(str(expires)) <= moment:
            continue
        asked[ask_id] = {
            "kind": "agent",
            "run_id": str(event.run_id),
            "task": title_of(title, objective),
            "ask_id": ask_id,
            "agent": payload.get("agent"),
            "reason": payload.get("title"),
            "options": list(payload.get("options") or []),
            "expires_at": expires,
        }
    return list(asked.values())
