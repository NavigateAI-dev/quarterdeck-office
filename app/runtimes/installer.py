"""Installing a build's packages, apart from everything else.

The coding agent never has network. When the owner lets builds use packages,
the agent lists them (package.json, requirements.txt) and the office installs
them here: a separate service with network but no secrets, that can reach the
build folders and nothing else, and that runs exactly two fixed commands, never
one it is handed:

- npm install with install scripts switched off, so no package's own code
  runs while installing;
- pip install of ready-made wheels only (no source builds, so again no package
  code runs), into py_packages/ inside the build.

The package code runs later only inside the agent's sandbox and the isolated
runner, neither of which has network.

Protocol, as the isolated runner's: the API writes install-jobs/<id>.job.json
and waits for install-jobs/<id>.result.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.runtimes.base import ExecutionError, ExecutionLimits, ExecutionResult, run_bounded
from app.runtimes.isolated import _inside, _write_atomic, _write_atomic_text

JOBS = "install-jobs"
HEARTBEAT = "installer.heartbeat"
HEARTBEAT_INTERVAL = 5.0
TIMEOUT_SECONDS = 600.0
# What a build may list, and the one command each becomes.
MANIFESTS = {"npm": "package.json", "pip": "requirements.txt"}
COMMANDS = {
    "npm": [
        "npm",
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
    ],
    "pip": [
        # The image's own Python, which has pip; the office's environment does not.
        "/usr/local/bin/python3",
        "-m",
        "pip",
        "install",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--target",
        "py_packages",
        "-r",
        "requirements.txt",
    ],
}
# Where the installed packages land, kept out of the build's commits.
INSTALLED = ("node_modules/", "py_packages/")


def wanted(folder: Path) -> list[str]:
    """Which installs a build asks for, by the manifests it wrote."""
    return [kind for kind, manifest in MANIFESTS.items() if (folder / manifest).is_file()]


class PackageInstaller:
    """API side: hands one install to the installer service and waits."""

    def __init__(self, root: Path, poll_seconds: float = 0.5, grace_seconds: float = 30.0) -> None:
        self.root = root
        self.poll_seconds = poll_seconds
        self.grace_seconds = grace_seconds

    async def install(self, kind: str, cwd: Path) -> ExecutionResult:
        if kind not in COMMANDS:
            raise ExecutionError("install_kind_unknown")
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
                "kind": kind,
                "cwd": str(cwd.resolve().relative_to(self.root.resolve())),
            },
        )
        deadline = asyncio.get_running_loop().time() + TIMEOUT_SECONDS + self.grace_seconds
        while asyncio.get_running_loop().time() < deadline:
            if result_path.exists():
                payload = json.loads(result_path.read_text())
                result_path.unlink(missing_ok=True)
                if payload.get("error"):
                    raise ExecutionError(str(payload["error"]))
                return ExecutionResult(
                    command=tuple(COMMANDS[kind]),
                    exit_code=int(payload["exit_code"]),
                    stdout=str(payload["stdout"]),
                    stderr=str(payload["stderr"]),
                    duration_seconds=float(payload["duration_seconds"]),
                    truncated=bool(payload["truncated"]),
                )
            await asyncio.sleep(self.poll_seconds)
        (jobs / f"{job_id}.job.json").unlink(missing_ok=True)
        raise ExecutionError("installer_unavailable")


async def serve_once(root: Path) -> bool:
    """Installer side: the oldest pending install, if any. True if one ran."""
    jobs = root / JOBS
    pending = sorted(jobs.glob("*.job.json"), key=lambda path: path.stat().st_mtime)
    if not pending:
        return False
    claimed = pending[0].with_suffix(".running")
    try:
        os.replace(pending[0], claimed)
    except FileNotFoundError:
        return False
    try:
        job = json.loads(claimed.read_text())
        job_id = str(job["id"])
        kind = str(job["kind"])
        if not job_id.isalnum():
            raise ValueError("job_id")
    except (OSError, ValueError, KeyError, TypeError):
        claimed.unlink(missing_ok=True)
        return True
    result_path = jobs / f"{job_id}.result.json"
    payload: dict[str, Any]
    try:
        if kind not in COMMANDS:
            raise ExecutionError("install_kind_unknown")
        cwd = root / str(job["cwd"])
        if not _inside(root, cwd) or (root / JOBS) in cwd.resolve().parents:
            raise ExecutionError("cwd_outside_root")
        if not (cwd / MANIFESTS[kind]).is_file():
            raise ExecutionError("manifest_missing")
        result = await run_bounded(
            list(COMMANDS[kind]),
            cwd.resolve(),
            ExecutionLimits(
                timeout_seconds=TIMEOUT_SECONDS,
                environment={
                    # The service's own tmpfs: it has nothing else writable.
                    "HOME": "/tmp",  # noqa: S108
                    "npm_config_cache": "/tmp/npm-cache",  # noqa: S108
                    "npm_config_update_notifier": "false",
                    "PIP_NO_INPUT": "1",
                },
            ),
        )
        payload = {
            "exit_code": result.exit_code,
            "stdout": result.stdout[-20_000:],
            "stderr": result.stderr[-20_000:],
            "duration_seconds": result.duration_seconds,
            "truncated": result.truncated,
        }
    except ExecutionError as error:
        payload = {"error": error.code}
    _write_atomic(result_path, payload)
    claimed.unlink(missing_ok=True)
    return True


def heartbeat_age(root: Path) -> float | None:
    try:
        return max(time.time() - float((root / HEARTBEAT).read_text()), 0.0)
    except (OSError, ValueError):
        return None


async def serve_forever(root: Path, poll_seconds: float = 0.5) -> None:
    (root / JOBS).mkdir(parents=True, exist_ok=True)
    last_beat = 0.0
    while True:
        if time.monotonic() - last_beat >= HEARTBEAT_INTERVAL or last_beat == 0.0:
            _write_atomic_text(root / HEARTBEAT, str(time.time()))
            last_beat = time.monotonic()
        if not await serve_once(root):
            await asyncio.sleep(poll_seconds)


def main() -> None:
    asyncio.run(serve_forever(Path(os.environ.get("INSTALLER_ROOT", "/engineering"))))


if __name__ == "__main__":
    main()
