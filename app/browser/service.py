"""The office's browser: Chromium, in a container that holds nothing worth stealing.

It has the internet and nothing else: no credentials, no database, no volume
but the exchange folder it is asked through, and a network of its own, so a
page cannot reach the office's services. Every request a page makes passes the
same check as an address the agent chose: public internet only. Each task's
session is a fresh browser context, with no cookies from anywhere, no
downloads and no device permissions; it is closed when the step ends, or when
left idle.

    jobs/<id>.job.json     {"id", "session", "do": "open"|"act"|"close", ...}
    jobs/<id>.result.json  {"page": {...}} or {"error": "<code>"}
    shots/<session>/<n>.jpg   each step's screenshot, for the task's record
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.browser.page import READ_PAGE
from app.browser.safety import SCHEMES, address_public, url_allowed

logger = logging.getLogger("quarterdeck.browser")

JOBS = "jobs"
SHOTS = "shots"
HEARTBEAT = "browser.heartbeat"
IDLE_SECONDS = 15 * 60
MAX_SESSIONS = 4
NAVIGATION_MS = 25_000
ACTION_MS = 10_000
VIEWPORT = {"width": 1280, "height": 800}
# What a page may load beyond the web itself: its own inline data.
INLINE = ("data", "blob", "about")


class Sessions:
    """One browser context per task step, closed when done or left idle."""

    def __init__(self, browser: Any, exchange: Path) -> None:
        self.browser = browser
        self.exchange = exchange
        self.open: dict[str, dict[str, Any]] = {}
        self.hosts: dict[str, bool] = {}

    def public(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in INLINE:
            return True
        if parts.scheme not in SCHEMES or not parts.hostname:
            return False
        host = parts.hostname
        if host not in self.hosts:
            self.hosts[host] = address_public(host)
        return self.hosts[host]

    async def _guard(self, route: Any) -> None:
        if self.public(route.request.url):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def start(self, session: str) -> dict[str, Any]:
        if len(self.open) >= MAX_SESSIONS:
            await self.close_idle(force_oldest=True)
        context = await self.browser.new_context(
            accept_downloads=False,
            viewport=VIEWPORT,
            permissions=[],
            locale="en-US",
            java_script_enabled=True,
            service_workers="block",
        )
        await context.route("**/*", self._guard)
        page = await context.new_page()
        held = {"context": context, "page": page, "used": time.monotonic(), "step": 0}

        def newest(opened: Any) -> None:
            # A link that opens a new window: the agent follows it there.
            held["page"] = opened

        context.on("page", newest)
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
        self.open[session] = held
        return held

    async def close(self, session: str) -> None:
        held = self.open.pop(session, None)
        if held is not None:
            try:
                await held["context"].close()
            except Exception:  # noqa: BLE001 - closing is best effort
                logger.warning('{"event":"browser_session_not_closed"}')

    async def close_idle(self, force_oldest: bool = False) -> None:
        now = time.monotonic()
        for session, held in list(self.open.items()):
            if now - held["used"] > IDLE_SECONDS:
                await self.close(session)
        if force_oldest and len(self.open) >= MAX_SESSIONS:
            oldest = min(self.open, key=lambda key: self.open[key]["used"])
            await self.close(oldest)

    async def snapshot(self, session: str, held: dict[str, Any]) -> dict[str, Any]:
        page = held["page"]
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=ACTION_MS)
        except Exception:  # noqa: BLE001 - a slow page is read as it stands
            logger.info('{"event":"browser_page_still_loading"}')
        if not self.public(page.url):
            # A redirect is followed inside the browser, past the request check;
            # where it landed is checked here, and a private address is left.
            await page.goto("about:blank")
            held["left"] = True
        await asyncio.sleep(0.6)
        read: dict[str, Any] = dict(await page.evaluate(READ_PAGE))
        held["step"] += 1
        folder = self.exchange / SHOTS / session
        folder.mkdir(parents=True, exist_ok=True)
        shot = folder / f"{held['step']}.jpg"
        try:
            await page.screenshot(path=str(shot), type="jpeg", quality=60, full_page=False)
            read["shot"] = f"{SHOTS}/{session}/{shot.name}"
        except Exception:  # noqa: BLE001 - a page without a picture is still a page
            read["shot"] = ""
        read["step"] = held["step"]
        return read

    async def act(self, session: str, job: dict[str, Any]) -> dict[str, Any]:
        held = self.open.get(session)
        if held is None:
            return {"error": "browser_session_gone"}
        held["used"] = time.monotonic()
        page = held["page"]
        kind = str(job.get("kind") or "")
        element = page.locator(f'[data-office-n="{int(job.get("element") or 0)}"]').first
        try:
            if kind == "goto":
                url = str(job.get("text") or "")
                if not url_allowed(url):
                    return {"error": "address_not_allowed"}
                await page.goto(url, timeout=NAVIGATION_MS, wait_until="domcontentloaded")
            elif kind == "click":
                await element.click(timeout=ACTION_MS)
            elif kind == "type":
                await element.fill(str(job.get("text") or ""), timeout=ACTION_MS)
                if job.get("submit"):
                    await element.press("Enter", timeout=ACTION_MS)
            elif kind == "select":
                await element.select_option(label=str(job.get("text") or ""), timeout=ACTION_MS)
            elif kind == "scroll":
                distance = -600 if job.get("direction") == "up" else 600
                await page.mouse.wheel(0, distance)
            elif kind == "back":
                await page.go_back(timeout=NAVIGATION_MS, wait_until="domcontentloaded")
            else:
                return {"error": "action_unknown"}
        except Exception as failed:  # noqa: BLE001 - told to the agent, which tries otherwise
            name = type(failed).__name__
            code = "action_timed_out" if "Timeout" in name else "action_failed"
            return {"error": code, "page": await self.snapshot(session, held)}
        page_now = await self.snapshot(session, held)
        if held.pop("left", False):
            return {"error": "address_not_allowed", "page": page_now}
        return {"page": page_now}

    async def handle(self, job: dict[str, Any]) -> dict[str, Any]:
        session = str(job.get("session") or "")
        if not session.isalnum() or len(session) > 64:
            return {"error": "session_invalid"}
        do = job.get("do")
        if do == "open":
            await self.close(session)
            held = await self.start(session)
            return {"page": await self.snapshot(session, held)}
        if do == "act":
            return await self.act(session, job)
        if do == "close":
            await self.close(session)
            return {"closed": True}
        return {"error": "do_unknown"}


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


async def serve_once(exchange: Path, sessions: Sessions) -> bool:
    jobs = exchange / JOBS
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
        if not job_id.isalnum():
            raise ValueError("job_id")
    except (OSError, ValueError, KeyError, TypeError):
        claimed.unlink(missing_ok=True)
        return True
    try:
        payload = await sessions.handle(job)
    except Exception:  # noqa: BLE001 - one page must never stop the browser
        payload = {"error": "browser_failed"}
    _write_atomic(jobs / f"{job_id}.result.json", payload)
    claimed.unlink(missing_ok=True)
    return True


async def serve_forever(exchange: Path, poll_seconds: float = 0.2) -> None:
    from playwright.async_api import async_playwright

    (exchange / JOBS).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-first-run", "--disable-extensions"],
        )
        sessions = Sessions(browser, exchange)
        tidied = time.monotonic()
        while True:
            (exchange / HEARTBEAT).write_text(str(time.time()))
            if time.monotonic() - tidied > 60:
                tidied = time.monotonic()
                await sessions.close_idle()
            if not await serve_once(exchange, sessions):
                await asyncio.sleep(poll_seconds)


def main() -> None:
    asyncio.run(serve_forever(Path(os.environ.get("BROWSER_EXCHANGE", "/browser-exchange"))))


if __name__ == "__main__":
    main()
