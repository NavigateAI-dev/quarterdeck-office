"""Upgrade the office without losing work, and without trusting the outcome.

    python3 -m scripts.upgrade

The order matters: stop taking new work, let what is running reach a step
boundary, take a backup, rebuild, migrate, smoke test, then take work again.

Two rules the tests hold to:

- no fresh backup, no upgrade. A backup that failed, or that wrote nothing
  newer than the moment the upgrade began, stops the run before anything is
  rebuilt.
- a failed smoke test fails the upgrade. The office stays paused and the
  rollback is printed; it is never reported as a success.

Standard library only, like the launcher.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.version import VERSION
from scripts.launch import BASE_URL, env_value, start_calendar_helper

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "backups"
SETTLE_TIMEOUT = 300
HEALTH_TIMEOUT = 180
BACKUP_COMMAND = ["docker", "compose", "exec", "-T", "api", "python", "-m", "scripts.backup"]
# The remote route shares the office container's network, so it is rebuilt
# and restarted with it.
BUILD_COMMAND = [
    "docker",
    "compose",
    "up",
    "-d",
    "--build",
    # A service no longer in the Compose file stops with the upgrade.
    "--remove-orphans",
    "api",
    "runner",
    "sources",
    "fetcher",
    "remote",
    # Installs a build's packages, when the owner allows them.
    "installer",
]

ROLLBACK = (
    "The office is left paused; unpause it under Settings once it is healthy.\n"
    "     To roll back, follow 'Rolling back' in docs/operations.md: it restores the\n"
    "     backup taken above into the previous version beside this office, never over it."
)


class UpgradeRefused(Exception):
    """A precondition failed. Nothing further was attempted."""


def call(
    path: str, token: str, method: str = "GET", body: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        f"{BASE_URL}{path}",
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return dict(json.load(response)) if response.status != 204 else {}


def run(command: list[str], timeout: int = 1800) -> int:
    return subprocess.run(command, cwd=ROOT, timeout=timeout, check=False).returncode  # noqa: S603


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=5) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def fresh_archive(backup_dir: Path, since: float) -> Path | None:
    """The newest archive written after `since`, or None if there is none.

    An archive from last week is not a backup of what is about to be migrated,
    so only one written during this run counts.
    """
    if not backup_dir.is_dir():
        return None
    written = [
        path
        for path in backup_dir.iterdir()
        if path.is_file()
        and path.suffix in {".tar", ".gz", ".tgz"}
        and path.stat().st_mtime >= since
    ]
    return max(written, key=lambda path: path.stat().st_mtime, default=None)


def take_backup(
    backup_dir: Path = BACKUPS,
    runner: Callable[[list[str]], int] = run,
    now: Callable[[], float] = time.time,
) -> Path:
    """Back up, and prove it landed. Raises UpgradeRefused if it did not."""
    started = now()
    code = runner(BACKUP_COMMAND)
    if code != 0:
        raise UpgradeRefused(f"the backup command failed (exit {code}); nothing was changed")
    archive = fresh_archive(backup_dir, started)
    if archive is None:
        raise UpgradeRefused(
            f"the backup command reported success but wrote no new archive to {backup_dir}; "
            "nothing was changed"
        )
    return archive


def smoke(read: Callable[[str], dict[str, Any]]) -> list[tuple[str, bool]]:
    """Ask the office to answer for itself, not merely to have started."""
    checks: list[tuple[str, bool]] = []
    try:
        status = read("/status")
        overview = read("/office/overview")
        setup = read("/setup")
    except Exception as error:  # noqa: BLE001 - any failure here is a failed smoke test
        return [(f"the office could not be read: {error}", False)]
    database = status.get("database") or {}
    checks.append(("database", database.get("state") == "ok"))
    checks.append(("schema revision", bool(database.get("revision"))))
    checks.append(("board counts", "counts" in overview))
    checks.append(("setup steps", "steps" in setup))
    return checks


def main() -> int:
    token = env_value(ROOT / ".env", "OWNER_API_TOKEN")
    if not token:
        print("FAIL: OWNER_API_TOKEN is not set in .env")
        return 1
    if not healthy():
        print("FAIL: the office is not answering; start it before upgrading")
        return 1

    print(f"upgrading to {VERSION}")
    print("1/6 taking no new work")
    call("/settings", token, "PUT", {"pause_new_tasks": True, "pause_schedules": True})

    print("2/6 letting work in flight reach a step boundary")
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        remaining = int(call("/office/overview", token)["counts"]["in_motion"])
        if remaining == 0:
            break
        print(f"     {remaining} task(s) still moving…")
        time.sleep(5)
    else:
        print("     still moving after the wait; they resume from their checkpoint afterwards")

    print("3/6 taking a backup")
    try:
        archive = take_backup()
    except UpgradeRefused as refused:
        print(f"FAIL: {refused}")
        print("     The office is still paused; unpause it under Settings.")
        return 1
    print(f"     {archive.name}")

    print("4/6 rebuilding and migrating")
    if run(BUILD_COMMAND) != 0:
        print(f"FAIL build.\n     {ROLLBACK}")
        return 1
    print("5/6 smoke test")
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while not healthy() and time.monotonic() < deadline:
        time.sleep(3)
    checks = smoke(lambda path: call(path, token))
    for name, passed in checks:
        print(f"     {'ok  ' if passed else 'FAIL'} {name}")
    if not all(passed for _, passed in checks):
        print(f"FAIL smoke test.\n     {ROLLBACK}")
        return 1

    print("6/6 taking work again")
    call("/settings", token, "PUT", {"pause_new_tasks": False, "pause_schedules": False})
    # New helper code takes effect; macOS keeps the owner's answer.
    start_calendar_helper(restart=True)
    print(f"ok   upgraded to {VERSION}; backup kept at backups/{archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
