"""CR1: a task that crashes ends as failed, and says why."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from app.orchestrator.runner import Dispatch
from app.runtime.job import Job
from app.runtime.worker import RunWorker
from tests.unit.test_config import settings


class Recorded:
    """What the worker records when a task crashes."""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.failed: list[tuple[Any, str | None, str, tuple[str, ...] | None]] = []
        self.released: list[Any] = []

    def add_event(self, run_id: Any, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append((kind, payload or {}))

    def fail(
        self,
        run_id: Any,
        node: str | None,
        code: str,
        only_from: tuple[str, ...] | None = None,
    ) -> bool:
        self.failed.append((run_id, node, code, only_from))
        return self.active

    def release_lease(self, run_id: Any) -> None:
        self.released.append(run_id)


class Crashing(RunWorker):
    def __init__(self, error: BaseException, repository: Recorded) -> None:
        super().__init__(settings(), repository, None)  # type: ignore[arg-type]
        self.error = error
        self.notified: list[tuple[Any, str]] = []

    def month_spent(self) -> bool:
        return False

    async def _execute(self, job: Job) -> None:
        raise self.error

    async def _notify(self, run_id: Any, status: str) -> None:
        self.notified.append((run_id, status))


def a_job() -> Job:
    return Job(
        dispatch=Dispatch(run_id=uuid4(), task_id=uuid4(), project_id=uuid4()),
        objective="Check the weather in Lyon",
        context={},
    )


async def run_one(worker: Crashing, job: Job) -> None:
    worker.queue.put_nowait(job)
    await worker._run(await worker.queue.get())


async def test_a_crash_fails_the_task_with_its_reason() -> None:
    repository = Recorded()
    worker = Crashing(ValueError("extra fields not permitted"), repository)
    job = a_job()
    await run_one(worker, job)
    [(run_id, _, code, only_from)] = repository.failed
    assert run_id == job.dispatch.run_id
    assert code == "unexpected_error:ValueError"
    assert only_from is not None and "RUNNING" in only_from and "COMPLETED" not in only_from
    [(kind, payload)] = repository.events
    assert kind == "run_failed" and payload["code"] == code
    assert "extra fields" in payload["detail"]
    assert repository.released == [job.dispatch.run_id]
    assert worker.notified == [(job.dispatch.run_id, "FAILED")]


async def test_a_crash_after_the_task_ended_changes_nothing() -> None:
    repository = Recorded(active=False)
    worker = Crashing(RuntimeError("after the end"), repository)
    await run_one(worker, a_job())
    assert repository.events == [] and worker.notified == []


async def test_a_shutdown_is_left_for_recovery() -> None:
    repository = Recorded()
    worker = Crashing(asyncio.CancelledError(), repository)
    with pytest.raises(asyncio.CancelledError):
        await run_one(worker, a_job())
    assert repository.failed == []
