import asyncio

from app.channels.inbound_office import supervise


async def test_one_listener_per_app_and_removed_apps_are_stopped() -> None:
    wanted: set[object] = {"office", "researcher"}
    started: list[object] = []
    stopped: list[object] = []
    stop = asyncio.Event()

    async def start(key: object, own_stop: asyncio.Event) -> None:
        started.append(key)
        try:
            await own_stop.wait()
        finally:
            stopped.append(key)

    task = asyncio.create_task(supervise(lambda: set(wanted), start, stop, interval=0.01))
    await asyncio.sleep(0.05)
    assert sorted(started) == ["office", "researcher"]  # started once each
    wanted.discard("researcher")
    await asyncio.sleep(0.05)
    assert stopped == ["researcher"]
    wanted.add("critic")
    await asyncio.sleep(0.05)
    assert started.count("critic") == 1 and started.count("office") == 1
    stop.set()
    await task
    assert sorted(stopped) == ["critic", "office", "researcher"]


async def test_a_listener_that_ends_is_restarted() -> None:
    runs: list[int] = []
    stop = asyncio.Event()

    async def start(key: object, own_stop: asyncio.Event) -> None:
        runs.append(1)  # returns immediately, like a crashed connection

    task = asyncio.create_task(supervise(lambda: {"office"}, start, stop, interval=0.01))
    await asyncio.sleep(0.06)
    stop.set()
    await task
    assert len(runs) >= 2
