"""Prove that a backup restores into a working office.

    python3 -m scripts.restore_drill [archive]

A restore that did not raise is not proof. This brings up a second, throwaway
stack — its own volumes, its own ports, the `quarterdeck-drill` compose project —
restores the archive into it, compares the result against the archive row
by row (every value, in any order) and file by file, asks the restored office
for its own status, and then destroys the stack.

The office running on this machine is never touched: the drill refuses to run
under the live project name, and mounts the backups directory read-only.

Standard library only, like the launcher.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from typing import Any

from scripts.launch import env_value

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "backups"
PROJECT = "quarterdeck-drill"
LIVE_PROJECT = "quarterdeck"
DRILL_URL = "http://127.0.0.1:8001"
HEALTH_TIMEOUT = 180

COMPOSE = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.drill.yml",
]


class DrillFailed(Exception):
    """A step of the drill did not do what it claims to do."""


def compose(*arguments: str, timeout: int = 1800) -> int:
    if PROJECT == LIVE_PROJECT:  # pragma: no cover - a guard against a bad edit
        raise DrillFailed("the drill would run against the live office")
    return subprocess.run([*COMPOSE, *arguments], cwd=ROOT, timeout=timeout, check=False).returncode  # noqa: S603


def newest_archive(backup_dir: Path = BACKUPS) -> Path:
    archives = [
        path for path in backup_dir.iterdir() if path.is_file() and path.name.endswith(".tar.gz")
    ]
    if not archives:
        raise DrillFailed(f"no archive to restore in {backup_dir}")
    return max(archives, key=lambda path: path.stat().st_mtime)


def drill_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{DRILL_URL}/health", timeout=5) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def wait_for_health(timeout: int = HEALTH_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if drill_healthy():
            return True
        time.sleep(3)
    return False


def status(token: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        f"{DRILL_URL}/status", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return dict(json.load(response))


def run_drill(archive: Path, token: str | None) -> None:
    """The drill itself. Prints each step as it happens; raises DrillFailed.

    Printed rather than collected so the step lines interleave with the output
    of the commands they run, instead of arriving after it.
    """
    say = partial(print, flush=True)
    say(f"archive: {archive.name}")
    inside = f"/backups/{archive.name}"

    say("1/5 bringing up a throwaway stack")
    if compose("up", "-d", "--build", "api") != 0:
        raise DrillFailed("the drill stack did not start")
    if not wait_for_health():
        raise DrillFailed("the drill office never became healthy")

    say("2/5 stopping its API so the workspace is untouched during restore")
    if compose("stop", "api") != 0:
        raise DrillFailed("could not stop the drill API")

    say("3/5 restoring")
    if compose("run", "--rm", "--no-deps", "api", "python", "-m", "scripts.restore", inside) != 0:
        raise DrillFailed("the restore was refused")

    say("4/5 comparing the restored office against the manifest")
    if (
        compose("run", "--rm", "--no-deps", "api", "python", "-m", "scripts.verify_restore", inside)
        != 0
    ):
        raise DrillFailed("the restored office does not match the archive")

    say("5/5 asking the restored office for its status")
    if compose("start", "api") != 0 or not wait_for_health():
        raise DrillFailed("the restored office did not come back up")
    if token:
        state = status(token)
        database = state.get("database") or {}
        if database.get("state") != "ok" or not database.get("revision"):
            raise DrillFailed(f"the restored office reports {database}")
        say(f"     version {state.get('version')}, schema {database.get('revision')}")
    else:
        say("     (no OWNER_API_TOKEN in .env; health checked, status skipped)")


def main() -> int:
    try:
        archive = Path(sys.argv[1]) if len(sys.argv) > 1 else newest_archive()
        if not archive.is_file():
            raise DrillFailed(f"no such archive: {archive}")
        token = env_value(ROOT / ".env", "OWNER_API_TOKEN")
        run_drill(archive, token)
    except DrillFailed as failure:
        # Left standing on purpose: a failed drill is worth looking at.
        print(f"FAIL {failure}")
        print("     The drill stack is left up for inspection:")
        print(f"     {' '.join(COMPOSE)} logs api")
        print(f"     Destroy it with: {' '.join(COMPOSE)} down -v")
        return 1
    print("6/6 destroying the drill stack")
    compose("down", "-v")
    print("ok   the archive restores into a working office")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
