"""Isolated execution of repository-controlled commands.

A test command is repository code. It must not run where application
credentials, the database, or unrelated workspace data are reachable. The API
therefore hands each command to a separate runner service through a shared
engineering volume, and the runner executes it with no network and no
secrets (see the `runner` service in docker-compose.yml).

Protocol: the API writes `jobs/<id>.job.json` atomically and waits for
`jobs/<id>.result.json`. The runner trusts nothing in a job: it re-resolves
the working directory inside its own root before running anything. If no
result arrives in time the command fails as `runner_unavailable`; it is never
run locally instead.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.runtimes.base import ExecutionError, ExecutionLimits, ExecutionResult, run_bounded

JOBS = "jobs"
HEARTBEAT = "runner.heartbeat"
HEARTBEAT_INTERVAL = 5.0


def _inside(root: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    base = root.resolve()
    return resolved != base and base in resolved.parents


def _write_atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_atomic_text(path, json.dumps(payload))


class IsolatedExecutor:
    """API side: submits a command to the runner and waits for its result."""

    def __init__(
        self, root: Path, poll_seconds: float = 0.25, response_grace_seconds: float = 30.0
    ) -> None:
        self.root = root
        self.poll_seconds = poll_seconds
        self.response_grace_seconds = response_grace_seconds

    async def run(
        self, command: list[str], cwd: Path, limits: ExecutionLimits | None = None
    ) -> ExecutionResult:
        bounds = limits or ExecutionLimits()
        if not command:
            raise ExecutionError("empty_command")
        if not _inside(self.root, cwd):
            raise ExecutionError("cwd_outside_root")
        jobs = self.root / JOBS
        jobs.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex
        result_path = jobs / f"{job_id}.result.json"
        _write_atomic(
            jobs / f"{job_id}.job.json",
            {
                "id": job_id,
                "cwd": str(cwd.resolve().relative_to(self.root.resolve())),
                "command": command,
                "timeout": bounds.timeout_seconds,
                "max_output": bounds.max_output_bytes,
            },
        )
        deadline = asyncio.get_running_loop().time() + bounds.timeout_seconds
        deadline += self.response_grace_seconds
        while asyncio.get_running_loop().time() < deadline:
            if result_path.exists():
                payload = json.loads(result_path.read_text())
                result_path.unlink(missing_ok=True)
                if payload.get("error"):
                    raise ExecutionError(str(payload["error"]))
                return ExecutionResult(
                    command=tuple(command),
                    exit_code=int(payload["exit_code"]),
                    stdout=str(payload["stdout"]),
                    stderr=str(payload["stderr"]),
                    duration_seconds=float(payload["duration_seconds"]),
                    truncated=bool(payload["truncated"]),
                )
            await asyncio.sleep(self.poll_seconds)
        # Withdraw the job so a runner that starts late does not execute it.
        (jobs / f"{job_id}.job.json").unlink(missing_ok=True)
        raise ExecutionError("runner_unavailable")


async def serve_once(root: Path) -> bool:
    """Runner side: execute the oldest pending job, if any. True if one ran."""
    jobs = root / JOBS
    pending = sorted(jobs.glob("*.job.json"), key=lambda path: path.stat().st_mtime)
    if not pending:
        return False
    job_path = pending[0]
    claimed = job_path.with_suffix(".running")
    try:
        os.replace(job_path, claimed)  # A withdrawn job is simply gone.
    except FileNotFoundError:
        return False
    try:
        job = json.loads(claimed.read_text())
        job_id = str(job["id"])
        if not job_id.isalnum():
            raise ValueError("job_id")
    except (OSError, ValueError, KeyError, TypeError):
        claimed.unlink(missing_ok=True)
        return True
    result_path = jobs / f"{job_id}.result.json"
    try:
        cwd = root / str(job["cwd"])
        if not _inside(root, cwd) or (root / JOBS) in cwd.resolve().parents:
            raise ExecutionError("cwd_outside_root")
        result = await run_bounded(
            [str(part) for part in job["command"]],
            cwd.resolve(),
            ExecutionLimits(
                timeout_seconds=float(job["timeout"]),
                max_output_bytes=int(job["max_output"]),
            ),
        )
        payload: dict[str, Any] = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
            "truncated": result.truncated,
        }
    except ExecutionError as error:
        payload = {"error": error.code}
    except (KeyError, TypeError, ValueError):
        payload = {"error": "invalid_job"}
    _write_atomic(result_path, payload)
    claimed.unlink(missing_ok=True)
    return True


def heartbeat_age(root: Path) -> float | None:
    """Seconds since the runner last reported, or None if it never has."""
    try:
        return max(time.time() - float((root / HEARTBEAT).read_text()), 0.0)
    except (OSError, ValueError):
        return None


async def serve_forever(root: Path, poll_seconds: float = 0.25) -> None:
    (root / JOBS).mkdir(parents=True, exist_ok=True)
    last_beat = 0.0
    while True:
        if time.monotonic() - last_beat >= HEARTBEAT_INTERVAL or last_beat == 0.0:
            _write_atomic_text(root / HEARTBEAT, str(time.time()))
            last_beat = time.monotonic()
        if not await serve_once(root):
            await asyncio.sleep(poll_seconds)


def main() -> None:
    root = Path(os.environ.get("RUNNER_ROOT", "/engineering"))
    asyncio.run(serve_forever(root))


if __name__ == "__main__":
    main()
