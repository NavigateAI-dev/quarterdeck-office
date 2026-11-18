"""What a chat thread remembers: the proposal waiting for a yes, and its last turns.

Held in memory, an office restart forgot both: the owner said yes to a proposal
and nothing happened, or asked a follow-up and the bot had lost the thread.
`DatabaseThreads` keeps them in the office's database, per app, channel and
thread, so each bot remembers its own threads and no other's.

Remembered across restarts, a proposal must not wait forever: a yes typed in
an old thread weeks later is not consent to that work now. A proposal lapses
after `PROPOSAL_SECONDS`.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ChannelThread

HISTORY_TURNS = 8
PROPOSAL_SECONDS = 24 * 60 * 60

Key = tuple[str, str | None]
# (project id, or None to create one; project name; the objective). A watch
# offered in the thread is (WATCH, its title, the watch as JSON).
Proposal = tuple[str | None, str, str]
WATCH = "watch"


class ThreadStore(Protocol):
    def propose(self, key: Key, proposal: Proposal) -> None: ...
    def take(self, key: Key) -> Proposal | None:
        """The waiting proposal, removed; None if there is none or it lapsed."""
        ...

    def turns(self, key: Key) -> list[dict[str, str]]: ...
    def remember(self, key: Key, question: str, answer: str) -> None: ...


class MemoryThreads:
    """For one process's lifetime. Used where there is no database, and in tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._proposals: dict[Key, tuple[float, Proposal]] = {}
        self._turns: dict[Key, deque[dict[str, str]]] = {}

    def propose(self, key: Key, proposal: Proposal) -> None:
        self._proposals[key] = (self.clock(), proposal)

    def take(self, key: Key) -> Proposal | None:
        held = self._proposals.pop(key, None)
        if held is None or self.clock() - held[0] > PROPOSAL_SECONDS:
            return None
        return held[1]

    def turns(self, key: Key) -> list[dict[str, str]]:
        return list(self._turns.get(key, ()))

    def remember(self, key: Key, question: str, answer: str) -> None:
        turns = self._turns.setdefault(key, deque(maxlen=HISTORY_TURNS))
        turns.append({"role": "user", "content": question})
        turns.append({"role": "assistant", "content": answer})


class DatabaseThreads:
    """One app's threads, kept in the office's database."""

    def __init__(self, session_factory: sessionmaker[Session], app_id: UUID) -> None:
        self.session_factory = session_factory
        self.app_id = app_id

    def _where(self, key: Key) -> Any:

        channel, thread = key
        return (
            ChannelThread.app_id == self.app_id,
            ChannelThread.channel_id == channel,
            ChannelThread.thread_key == (thread or ""),
        )

    def _upsert(self, session: Session, key: Key, values: dict[str, Any]) -> None:
        from app.db.models import ChannelThread

        channel, thread = key
        # A core insert: the id the ORM would assign is given here instead.
        statement = insert(ChannelThread).values(
            id=uuid4(), app_id=self.app_id, channel_id=channel, thread_key=thread or "", **values
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ChannelThread.app_id,
                    ChannelThread.channel_id,
                    ChannelThread.thread_key,
                ],
                set_={**values, "updated_at": datetime.now(UTC)},
            )
        )

    def propose(self, key: Key, proposal: Proposal) -> None:
        with self.session_factory() as session, session.begin():
            self._upsert(
                session,
                key,
                {"proposal_json": list(proposal), "proposed_at": datetime.now(UTC)},
            )

    def take(self, key: Key) -> Proposal | None:
        from app.db.models import ChannelThread

        with self.session_factory() as session, session.begin():
            # Locked, so two quick yeses start the work once.
            row = session.scalar(select(ChannelThread).where(*self._where(key)).with_for_update())
            if row is None or not row.proposal_json:
                return None
            held, asked = list(row.proposal_json), row.proposed_at
            row.proposal_json = None
            row.proposed_at = None
        if asked is None or datetime.now(UTC) - asked > timedelta(seconds=PROPOSAL_SECONDS):
            return None
        project_id, name, objective = held
        return (project_id, str(name), str(objective))

    def turns(self, key: Key) -> list[dict[str, str]]:
        from app.db.models import ChannelThread

        with self.session_factory() as session:
            row = session.scalar(select(ChannelThread).where(*self._where(key)))
            return [dict(turn) for turn in (row.turns_json if row else None) or []]

    def remember(self, key: Key, question: str, answer: str) -> None:
        from app.db.models import ChannelThread

        with self.session_factory() as session, session.begin():
            row = session.scalar(select(ChannelThread).where(*self._where(key)).with_for_update())
            turns = list(row.turns_json or []) if row is not None else []
            turns += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
            self._upsert(session, key, {"turns_json": turns[-HISTORY_TURNS:]})
