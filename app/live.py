"""The office live: what is written, heard as it is written.

Postgres sends a notice for every new run event, thread message and run
control, and every run whose status changes (migration 0069), on the channel
`office_<schema>`. One listening connection hears them and hands each to every
open stream; a browser's stream (`GET /stream`) redraws what the notice names
instead of asking again and again.

A notice names the table, the row, the run and the thread, never the content:
the page reads what it may see through the routes it always used. When the
connection drops, each stream is told to read everything again (`resync`),
then the hub listens again after a growing pause.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import psycopg

from app.orchestrator.checkpoints import checkpoint_dsn

logger = logging.getLogger(__name__)

# A stream that falls this far behind is told to read everything again.
QUEUE_SIZE = 256
RETRY_AFTER = (1.0, 2.0, 5.0, 10.0, 30.0)
KEEP_ALIVE_SECONDS = 15.0
CONNECT_SECONDS = 10
RESYNC = {"table": "resync"}


class LiveHub:
    """One LISTEN connection, fanned out to each open stream."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self.connected = False
        self.listened = False

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
        self.listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.listeners.discard(queue)

    def publish(self, notice: dict[str, Any]) -> None:
        for queue in list(self.listeners):
            try:
                queue.put_nowait(notice)
            except asyncio.QueueFull:
                # Too far behind to catch up notice by notice: start over.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(RESYNC)

    async def run(self, stop: asyncio.Event) -> None:
        """Listen until stopped, listening again after each dropped connection."""
        failures = 0
        while not stop.is_set():
            try:
                await self._listen(stop)
                failures = 0
            except Exception as error:  # a dropped connection, a restart of Postgres
                self.connected = False
                self.publish(RESYNC)
                wait = RETRY_AFTER[min(failures, len(RETRY_AFTER) - 1)]
                failures += 1
                logger.warning(
                    json.dumps(
                        {
                            "event": "live_listen_failed",
                            "error": type(error).__name__,
                            "retry_in": wait,
                        }
                    )
                )
                try:
                    await asyncio.wait_for(stop.wait(), wait)
                except TimeoutError:
                    pass

    async def _listen(self, stop: asyncio.Event) -> None:
        async with await psycopg.AsyncConnection.connect(
            checkpoint_dsn(self.database_url), autocommit=True, connect_timeout=CONNECT_SECONDS
        ) as conn:
            row = await (await conn.execute("SELECT current_schema()")).fetchone()
            schema = str(row[0]) if row else "public"
            await conn.execute(f'LISTEN "office_{schema}"')
            self.connected = True
            if self.listened:
                # Listening again: whatever was written meanwhile is read again.
                self.publish(RESYNC)
            self.listened = True
            try:
                # Heard in this task, a second at a time, so a stop is seen within
                # a second and closing never waits on a read still going.
                while not stop.is_set():
                    async for notice in conn.notifies(timeout=1.0):
                        self.publish(_parse(notice.payload))
                        if stop.is_set():
                            break
            finally:
                self.connected = False


def _parse(payload: str) -> dict[str, Any]:
    try:
        notice = json.loads(payload)
    except ValueError:
        return dict(RESYNC)
    return notice if isinstance(notice, dict) else dict(RESYNC)


async def event_stream(
    hub: LiveHub,
    disconnected: Callable[[], Awaitable[bool]],
    keep_alive: float = KEEP_ALIVE_SECONDS,
) -> AsyncIterator[str]:
    """The Server-Sent Events a browser's stream reads, until it goes away."""
    queue = hub.subscribe()
    try:
        # Reconnect after 3 s if dropped; the page reads everything again on hello.
        yield "retry: 3000\nevent: hello\ndata: {}\n\n"
        while not await disconnected():
            try:
                notice = await asyncio.wait_for(queue.get(), keep_alive)
            except TimeoutError:
                yield ": still here\n\n"
                continue
            yield f"data: {json.dumps(notice, separators=(',', ':'))}\n\n"
    finally:
        hub.unsubscribe(queue)
