"""The isolated test runner protocol, exercised in-process with temp dirs."""

import asyncio
import sys
from pathlib import Path

import pytest

from app.runtimes.base import ExecutionError, ExecutionLimits
from app.runtimes.isolated import IsolatedExecutor, serve_once


def attempt(root: Path) -> Path:
    directory = root / "attempt-1" / "repo"
    directory.mkdir(parents=True)
    return directory


async def run_with_runner(root: Path, command: list[str], cwd: Path, **limits: float) -> object:
    executor = IsolatedExecutor(root, poll_seconds=0.02, response_grace_seconds=5)

    async def runner() -> None:
        while not await serve_once(root):
            await asyncio.sleep(0.02)

    task = asyncio.create_task(runner())
    try:
        return await executor.run(command, cwd, ExecutionLimits(**limits))  # type: ignore[arg-type]
    finally:
        task.cancel()


async def test_a_job_round_trips_through_the_runner(tmp_path: Path) -> None:
    cwd = attempt(tmp_path)
    (cwd / "marker.txt").write_text("inside")
    result = await run_with_runner(
        tmp_path, [sys.executable, "-c", "print(open('marker.txt').read())"], cwd
    )
    assert result.succeeded  # type: ignore[attr-defined]
    assert result.stdout.strip() == "inside"  # type: ignore[attr-defined]


async def test_a_failing_test_is_reported_as_failure(tmp_path: Path) -> None:
    result = await run_with_runner(
        tmp_path, [sys.executable, "-c", "raise SystemExit(4)"], attempt(tmp_path)
    )
    assert result.exit_code == 4  # type: ignore[attr-defined]


async def test_a_timeout_in_the_runner_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="execution_timeout"):
        await run_with_runner(
            tmp_path,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            attempt(tmp_path),
            timeout_seconds=0.5,
        )


async def test_work_outside_the_engineering_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "engineering"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ExecutionError, match="cwd_outside_root"):
        await IsolatedExecutor(root).run([sys.executable, "-c", "1"], outside, ExecutionLimits())


async def test_a_symlink_cannot_lead_the_runner_outside_its_root(tmp_path: Path) -> None:
    root = tmp_path / "engineering"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ExecutionError, match="cwd_outside_root"):
        await IsolatedExecutor(root).run(
            [sys.executable, "-c", "1"], root / "link", ExecutionLimits()
        )


async def test_the_runner_revalidates_a_tampered_job(tmp_path: Path) -> None:
    """The runner trusts nothing in a job file, including its directory."""
    import json

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "evil.job.json").write_text(
        json.dumps(
            {"id": "evil", "cwd": "../../", "command": ["true"], "timeout": 5, "max_output": 100}
        )
    )
    assert await serve_once(tmp_path)
    result = json.loads((jobs / "evil.result.json").read_text())
    assert result["error"] == "cwd_outside_root"


async def test_no_runner_fails_instead_of_running_locally(tmp_path: Path) -> None:
    executor = IsolatedExecutor(tmp_path, poll_seconds=0.02, response_grace_seconds=0.2)
    with pytest.raises(ExecutionError, match="runner_unavailable"):
        await executor.run(
            [sys.executable, "-c", "1"], attempt(tmp_path), ExecutionLimits(timeout_seconds=0.1)
        )


async def test_the_runner_reports_a_heartbeat_while_serving(tmp_path: Path) -> None:
    from app.runtimes.isolated import HEARTBEAT, heartbeat_age, serve_forever

    assert heartbeat_age(tmp_path) is None
    task = asyncio.create_task(serve_forever(tmp_path, poll_seconds=0.02))
    try:
        for _ in range(50):
            if (tmp_path / HEARTBEAT).exists():
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
    age = heartbeat_age(tmp_path)
    assert age is not None and age < 5
