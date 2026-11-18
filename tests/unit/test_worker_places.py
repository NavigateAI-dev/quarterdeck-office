"""MW: the worker runs several tasks at once, as many as the owner allows."""

import asyncio
from typing import Any
from uuid import uuid4

from app.orchestrator.runner import Dispatch
from app.runtime.job import Job
from app.runtime.worker import RunWorker
from tests.unit.test_config import settings


class Recorded:
    """Only what the worker's own bookkeeping touches."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, str, dict[str, Any]]] = []
        self.stopped: list[tuple[Any, str, str | None]] = []

    def add_event(self, run_id: Any, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append((run_id, kind, payload or {}))

    def stop(
        self,
        run_id: Any,
        status: str,
        only_from: tuple[str, ...] | None = None,
        reason: str | None = None,
    ) -> bool:
        self.stopped.append((run_id, status, reason))
        return True


class Timed(RunWorker):
    """A worker whose tasks take a moment and say when they ran."""

    def __init__(self, places: int, over_limit: bool = False, **config: Any) -> None:
        super().__init__(settings(**config), Recorded(), None)  # type: ignore[arg-type]
        self.places = lambda: places  # type: ignore[method-assign]
        self.over_limit = over_limit
        self.log: list[tuple[str, str]] = []
        self.release = asyncio.Event()

    def month_spent(self) -> bool:
        return self.over_limit

    async def _execute(self, job: Job) -> None:
        name = job.objective
        self.log.append(("start", name))
        await self.release.wait()
        self.log.append(("end", name))


def job(name: str) -> Job:
    return Job(
        dispatch=Dispatch(run_id=uuid4(), task_id=uuid4(), project_id=uuid4()),
        objective=name,
        context={},
    )


async def started(worker: Timed, count: int) -> None:
    for _ in range(200):
        if sum(1 for kind, _ in worker.log if kind == "start") >= count:
            return
        await asyncio.sleep(0.01)


async def test_tasks_run_together_up_to_the_places_allowed() -> None:
    worker = Timed(places=2)
    worker.start()
    for name in ("a", "b", "c"):
        worker.submit(job(name))
    await started(worker, 2)
    await asyncio.sleep(0.05)
    assert worker.log == [("start", "a"), ("start", "b")], "the third waits for a place"
    assert worker.running_count == 2 and worker.waiting_count == 1
    worker.release.set()
    await asyncio.wait_for(worker.queue.join(), 2)
    assert [name for kind, name in worker.log if kind == "end"] == ["a", "b", "c"]
    await worker.stop()


async def test_one_place_runs_them_one_after_another() -> None:
    worker = Timed(places=1)
    worker.start()
    worker.submit(job("a"))
    worker.submit(job("b"))
    await started(worker, 1)
    await asyncio.sleep(0.05)
    assert worker.log == [("start", "a")]
    worker.release.set()
    await asyncio.wait_for(worker.queue.join(), 2)
    assert worker.log == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]
    await worker.stop()


async def test_more_places_given_while_tasks_wait_are_used() -> None:
    allowed = {"places": 1}
    worker = Timed(places=1)
    worker.places = lambda: allowed["places"]  # type: ignore[method-assign]
    worker.start()
    worker.submit(job("a"))
    worker.submit(job("b"))
    await started(worker, 1)
    allowed["places"] = 2
    await started(worker, 2)
    assert worker.running_count == 2
    worker.release.set()
    await asyncio.wait_for(worker.queue.join(), 2)
    await worker.stop()


async def test_stopping_the_worker_stops_every_task_it_runs() -> None:
    worker = Timed(places=3)
    worker.start()
    for name in ("a", "b"):
        worker.submit(job(name))
    await started(worker, 2)
    await worker.stop()
    assert worker.running_count == 0
    assert not any(kind == "end" for kind, _ in worker.log)


async def test_a_task_that_reaches_the_front_past_the_months_limit_pauses() -> None:
    worker = Timed(places=2, over_limit=True)
    worker.start()
    queued = job("a")
    worker.submit(queued)
    await asyncio.wait_for(worker.queue.join(), 2)
    assert worker.log == [], "no model is called"
    recorded = worker.repository
    assert recorded.stopped == [  # type: ignore[attr-defined]
        (queued.dispatch.run_id, "PAUSED", "monthly_limit_reached")
    ]
    await worker.stop()


def test_how_many_at_once_is_configured_and_bounded() -> None:
    assert settings().max_concurrent_runs == 3
    assert settings(max_concurrent_runs=5).max_concurrent_runs == 5
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(max_concurrent_runs=9)
