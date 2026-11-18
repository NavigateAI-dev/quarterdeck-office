"""The live hub hands each notice to every open stream."""

import asyncio
import json

from app.live import QUEUE_SIZE, RESYNC, LiveHub, event_stream

NOTICE = {"table": "run_events", "id": "7", "run": "r1", "topic": "t1"}


def test_each_open_stream_hears_each_notice() -> None:
    hub = LiveHub("postgresql+psycopg://office:secret@localhost/office")
    first, second = hub.subscribe(), hub.subscribe()

    hub.publish(NOTICE)

    assert first.get_nowait() == NOTICE and second.get_nowait() == NOTICE


def test_a_closed_stream_hears_nothing_more() -> None:
    hub = LiveHub("postgresql+psycopg://office:secret@localhost/office")
    queue = hub.subscribe()
    hub.unsubscribe(queue)

    hub.publish(NOTICE)

    assert queue.empty()


def test_a_stream_too_far_behind_is_told_to_read_everything_again() -> None:
    hub = LiveHub("postgresql+psycopg://office:secret@localhost/office")
    queue = hub.subscribe()
    for _ in range(QUEUE_SIZE + 1):
        hub.publish(NOTICE)

    assert queue.qsize() == 1 and queue.get_nowait() == RESYNC


async def test_the_stream_says_hello_then_each_notice_and_keeps_the_line_open() -> None:
    hub = LiveHub("postgresql+psycopg://office:secret@localhost/office")
    gone = False

    async def disconnected() -> bool:
        return gone

    lines = event_stream(hub, disconnected, keep_alive=0.05)
    assert "event: hello" in await anext(lines)
    hub.publish(NOTICE)
    sent = await anext(lines)
    assert json.loads(sent.removeprefix("data: ")) == NOTICE
    assert (await anext(lines)).startswith(":"), "a comment keeps the connection open"
    gone = True
    await asyncio.sleep(0)
    assert [line async for line in lines] == []
    assert hub.listeners == set(), "the stream lets go of its queue when the page goes"
