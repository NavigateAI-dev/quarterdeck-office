"""The office's side of the browser exchange: ask, wait, read the page.

The office never opens a browser or a socket for one: it writes a job into
the exchange folder and reads the answer, as it does with the fetcher.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.browser.service import HEARTBEAT, JOBS

# How long a step may take: a slow page is given time, a stuck one is not.
WAIT_SECONDS = 60.0
# A browser that has not said it is there in this long is not running.
GONE_SECONDS = 30.0


class BrowserUnavailable(Exception):
    """The browser did not answer; the code says why."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExchangeBrowser:
    """One session of the office's browser, for one step of one task."""

    def __init__(self, exchange: Path, session: str | None = None, poll: float = 0.15) -> None:
        self.exchange = exchange
        self.session = session or uuid4().hex
        self.poll = poll

    def running(self) -> bool:
        try:
            age = time.time() - float((self.exchange / HEARTBEAT).read_text())
        except (OSError, ValueError):
            return False
        return age < GONE_SECONDS

    async def _ask(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.running():
            raise BrowserUnavailable("browser_not_running")
        jobs = self.exchange / JOBS
        jobs.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex
        temporary = jobs / f".{job_id}.tmp"
        temporary.write_text(json.dumps({"id": job_id, "session": self.session, **body}))
        os.replace(temporary, jobs / f"{job_id}.job.json")
        result = jobs / f"{job_id}.result.json"
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if result.exists():
                payload: dict[str, Any] = json.loads(result.read_text())
                result.unlink(missing_ok=True)
                return payload
            await asyncio.sleep(self.poll)
        (jobs / f"{job_id}.job.json").unlink(missing_ok=True)
        raise BrowserUnavailable("browser_timed_out")

    async def open(self) -> dict[str, Any]:
        return await self._ask({"do": "open"})

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        return await self._ask({"do": "act", **action})

    async def close(self) -> None:
        try:
            await self._ask({"do": "close"})
        except BrowserUnavailable:
            pass

    def shot(self, page: dict[str, Any]) -> bytes | None:
        """The screenshot a step took, read from the exchange folder."""
        name = str(page.get("shot") or "")
        if not name or ".." in name:
            return None
        try:
            return (self.exchange / name).read_bytes()
        except OSError:
            return None
