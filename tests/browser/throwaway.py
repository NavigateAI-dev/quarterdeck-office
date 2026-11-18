"""Start and stop a throwaway office for a browser test run.

The suites change data as they go, so they run against an office of their own
(`seeded_office.py`) and never against one in use.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
BACKUPS = HERE.parents[1] / "backups"


def newest_backup(folder: Path = BACKUPS) -> Path | None:
    """The office's newest backup, for a copy with real work in it."""
    if not folder.is_dir():
        return None
    archives = [p for p in folder.iterdir() if p.name.endswith(".tar.gz") and p.is_file()]
    return max(archives, key=lambda p: p.stat().st_mtime) if archives else None


@contextmanager
def running_office(archive: Path | None = None) -> Iterator[dict[str, Any]]:
    """An office on a free port: its address, owner token and what it holds."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [sys.executable, str(HERE / "seeded_office.py"), str(port)]
    if archive is not None:
        command.append(str(archive))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)  # noqa: S603
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("the throwaway office did not start")
        office = {**json.loads(line), "base": f"http://127.0.0.1:{port}"}
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"{office['base']}/health", timeout=2):  # noqa: S310
                    break
            except OSError:
                time.sleep(0.5)
        yield office
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.stdout is not None:
            process.stdout.close()


def sign_in_url(office: dict[str, Any]) -> str:
    """A one-time code, exactly as the launcher mints one."""
    request = urllib.request.Request(  # noqa: S310 - loopback
        f"{office['base']}/session/code",
        method="POST",
        headers={"Authorization": f"Bearer {office['token']}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return f"{office['base']}/#code={json.load(response)['code']}"


def database_ready() -> bool:
    return bool(os.environ.get("TEST_DATABASE_URL"))
