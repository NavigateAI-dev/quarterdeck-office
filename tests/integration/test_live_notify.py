"""The office hears what is written as it is written.

A row added to a run's events, a thread's messages or a run's controls, or a
run's status changing, sends one small notice naming the run and the thread,
from whichever process wrote it. Nothing in the notice is the content itself.
"""

import asyncio
import json
from typing import Any

import psycopg
import pytest

from app.db.models import RoomMessage, Run
from app.orchestrator.checkpoints import checkpoint_dsn
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def schema_of(factory: Any) -> str:
    return str(factory.kw["bind"].get_execution_options()["schema_translate_map"][None])


async def heard(url: str, schema: str, write: Any) -> list[dict[str, Any]]:
    """Listen on the schema's channel, do the write, and return the notices."""
    notices: list[dict[str, Any]] = []
    async with await psycopg.AsyncConnection.connect(checkpoint_dsn(url), autocommit=True) as conn:
        await conn.execute(f'LISTEN "office_{schema}"')
        await asyncio.to_thread(write)
        try:
            async with asyncio.timeout(2):
                async for notice in conn.notifies():
                    notices.append(json.loads(notice.payload))
        except TimeoutError:
            pass
    return notices


async def test_a_run_event_names_its_run_and_thread(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    with factory() as session, session.begin():
        run = session.get(Run, dispatch.run_id)
        assert run is not None
        run.topic_id = __import__("uuid").uuid4()
        topic = str(run.topic_id)

    notices = await heard(
        url, schema_of(factory), lambda: repository.add_event(dispatch.run_id, "step_started")
    )

    assert notices == [
        {"table": "run_events", "id": notices[0]["id"], "run": str(dispatch.run_id), "topic": topic}
    ]


async def test_a_thread_message_names_its_thread(environment: Any) -> None:  # noqa: F811
    _, factory, project_id, url = environment
    topic = __import__("uuid").uuid4()

    def say() -> None:
        with factory() as session, session.begin():
            session.add(
                RoomMessage(
                    project_id=project_id,
                    topic_id=topic,
                    author_kind="owner",
                    author_name="Owner",
                    body="Hello",
                )
            )

    [notice] = await heard(url, schema_of(factory), say)
    assert (notice["table"], notice["topic"], notice["run"]) == ("room_messages", str(topic), None)
    assert "Hello" not in json.dumps(notice), "the notice never carries the content"


async def test_a_control_and_a_status_change_are_heard(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")

    controls = await heard(
        url, schema_of(factory), lambda: repository.add_control(dispatch.run_id, "pause")
    )
    assert {"run_controls", "run_events"} <= {n["table"] for n in controls}

    def finish() -> None:
        with factory() as session, session.begin():
            run = session.get(Run, dispatch.run_id)
            assert run is not None
            run.status = "CANCELLED"

    [status] = await heard(url, schema_of(factory), finish)
    assert (status["table"], status["run"]) == ("runs", str(dispatch.run_id))


async def test_an_unchanged_status_is_not_heard(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")

    def touch() -> None:
        with factory() as session, session.begin():
            run = session.get(Run, dispatch.run_id)
            assert run is not None
            run.state_json = {"plan": {}}

    assert await heard(url, schema_of(factory), touch) == []


async def next_notice(queue: Any, table: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            notice = await queue.get()
            if notice["table"] == table:
                return dict(notice)


async def test_the_hub_hears_a_write_and_hands_it_to_a_stream(environment: Any) -> None:  # noqa: F811
    from app.live import LiveHub

    repository, factory, project_id, url = environment
    schema = schema_of(factory)
    hub = LiveHub(f"{url}?options=-csearch_path%3D{schema}")
    queue = hub.subscribe()
    stop = asyncio.Event()
    listening = asyncio.create_task(hub.run(stop))
    try:
        async with asyncio.timeout(5):
            while not hub.connected:
                await asyncio.sleep(0.05)
        dispatch = repository.create(project_id, "Assess demand")
        await asyncio.to_thread(repository.add_event, dispatch.run_id, "step_started")
        heard = await next_notice(queue, "run_events")
        assert heard["run"] == str(dispatch.run_id)
    finally:
        stop.set()
        await listening


async def test_a_dropped_connection_tells_the_streams_to_read_again(environment: Any) -> None:  # noqa: F811
    from app.live import LiveHub

    repository, factory, project_id, url = environment
    schema = schema_of(factory)
    hub = LiveHub(f"{url}?options=-csearch_path%3D{schema}")
    queue = hub.subscribe()
    stop = asyncio.Event()
    listening = asyncio.create_task(hub.run(stop))
    try:
        async with asyncio.timeout(5):
            while not hub.connected:
                await asyncio.sleep(0.05)
        async with await psycopg.AsyncConnection.connect(
            checkpoint_dsn(url), autocommit=True
        ) as admin:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE query LIKE %s AND pid <> pg_backend_pid()",
                (f'LISTEN "office_{schema}"%',),
            )
        await next_notice(queue, "resync")
        # It listens again, and hears what is written after.
        async with asyncio.timeout(10):
            while not hub.connected:
                await asyncio.sleep(0.05)
        dispatch = repository.create(project_id, "Assess demand")
        await asyncio.to_thread(repository.add_event, dispatch.run_id, "step_started")
        assert (await next_notice(queue, "run_events"))["run"] == str(dispatch.run_id)
    finally:
        stop.set()
        await listening


async def test_the_stream_is_the_owners_only(client: Any) -> None:  # noqa: F811
    from app.live import LiveHub

    response = await client.get("/stream", headers={"Authorization": ""})
    assert response.status_code == 401
    assert isinstance(client.app_state.live, LiveHub), "the office starts listening with the app"
