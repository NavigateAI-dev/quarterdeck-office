"""The office's side of the fetch exchange: ask, wait, and read the page."""

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.browser.client import BrowserUnavailable, ExchangeBrowser
from app.tools.fetch import MAX_EXCERPT_CHARS, FetchError, Page
from app.tools.fetch_service import EXCHANGE_JOBS, HEARTBEAT

# Pages read at once for one step; the fetch service reads as many.
CONCURRENT_READS = 5
# Pages one task may open in the office's own browser when nothing else read them.
BROWSER_READS = 4
# Failures a browser may get past: a refusal, a page with nothing but script.
RENDERABLE = ("fetch_rejected_", "empty_page", "fetch_failed", "fetch_timeout")
# Shorter than this, a page is probably a shell its script fills in.
THIN_PAGE = 300


class ExchangeFetcher:
    """Reads pages through the fetch service, and never opens a socket itself.

    One per task: a page read once is kept for the rest of the task, and
    a step's pages are read several at a time. A page the fetch service cannot
    read, and one that is an empty shell until its script runs, is opened in
    the office's own browser, never a new outside service. Each read is
    reported to `on_read`: how, how long, how much, or why it failed.
    """

    def __init__(
        self,
        exchange: Path,
        timeout_seconds: float = 60.0,
        poll: float = 0.1,
        browser_exchange: Path | None = None,
        on_read: Callable[[dict[str, Any]], object] | None = None,
    ) -> None:
        self.exchange = exchange
        self.timeout_seconds = timeout_seconds
        self.poll = poll
        self.browser_exchange = browser_exchange
        self.on_read = on_read
        self.cache: dict[tuple[str, int, int], Page] = {}
        self.rendered = 0
        self.reads = asyncio.Semaphore(CONCURRENT_READS)

    async def __aenter__(self) -> "ExchangeFetcher":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def fetch(
        self, url: str, follow_redirects: int = 0, max_chars: int = MAX_EXCERPT_CHARS
    ) -> Page:
        key = (url, follow_redirects, max_chars)
        if key in self.cache:
            return self.cache[key]
        began = time.monotonic()
        try:
            page = await self._read(url, follow_redirects, max_chars)
        except FetchError as error:
            rendered = await self._render(url, max_chars, error.code)
            if rendered is None:
                self._report(url, began, None, error.code)
                raise
            page = rendered
        else:
            if len(page.text) < THIN_PAGE and page.read_via == "direct":
                page = await self._render(url, max_chars, "thin_page") or page
        self._report(url, began, page, None)
        self.cache[key] = page
        return page

    def _report(self, url: str, began: float, page: Page | None, code: str | None) -> None:
        if self.on_read is None:
            return
        from urllib.parse import urlsplit

        try:
            self.on_read(
                {
                    "host": (urlsplit(url).hostname or "")[:100],
                    "via": page.read_via if page is not None else "failed",
                    "ok": page is not None,
                    "ms": round((time.monotonic() - began) * 1000),
                    "chars": len(page.text) if page is not None else 0,
                    "code": code,
                }
            )
        except Exception:  # noqa: BLE001 - measuring never fails a read
            return

    async def _render(self, url: str, max_chars: int, code: str) -> Page | None:
        """The page in the office's own browser, when the fetch service could not read it."""
        if self.browser_exchange is None or self.rendered >= BROWSER_READS:
            return None
        if code != "thin_page" and not code.startswith(RENDERABLE):
            return None

        browser = ExchangeBrowser(self.browser_exchange)
        if not browser.running():
            return None
        self.rendered += 1
        try:
            await browser.open()
            moved = await browser.act({"kind": "goto", "text": url})
            shown = dict(moved.get("page") or {})
            text = str(shown.get("text") or "").strip()
            if moved.get("error") and not text:
                return None
        except BrowserUnavailable:
            return None
        finally:
            await browser.close()
        if len(text) < 40:
            return None
        return Page(url=str(shown.get("url") or url), text=text[:max_chars], read_via="browser")

    async def _read(self, url: str, follow_redirects: int, max_chars: int) -> Page:
        async with self.reads:
            payload = await self._request(
                {"url": url, "follow_redirects": follow_redirects, "max_chars": max_chars}
            )
        if payload.get("error"):
            raise FetchError(str(payload["error"]))
        page = payload["page"]
        return Page(
            url=str(page["url"]),
            text=str(page["text"]),
            retrieved_at=datetime.fromisoformat(page["retrieved_at"])
            if page.get("retrieved_at")
            else datetime.now(UTC),
            links=tuple((str(text), str(href)) for text, href in page.get("links") or []),
            read_via=str(page.get("read_via") or "direct"),
        )

    async def probe(self) -> list[dict[str, Any]]:
        """What the office can reach, as the fetch service finds it now."""
        payload = await self._request({"probe": True})
        if payload.get("error"):
            raise FetchError(str(payload["error"]))
        return list(payload.get("reach") or [])

    async def fetch_many(self, urls: list[str]) -> list[Page]:
        """Read what is reachable, several at a time, in the order asked.
        A page that fails is simply not evidence."""

        async def one(url: str) -> Page | None:
            try:
                return await self.fetch(url)
            except FetchError:
                return None

        read = await asyncio.gather(*(one(url) for url in urls))
        return [page for page in read if page is not None]

    async def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        jobs = self.exchange / EXCHANGE_JOBS
        jobs.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex
        temporary = jobs / f".{job_id}.tmp"
        temporary.write_text(json.dumps({"id": job_id, **body}))
        os.replace(temporary, jobs / f"{job_id}.job.json")
        result = jobs / f"{job_id}.result.json"
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if result.exists():
                payload: dict[str, Any] = json.loads(result.read_text())
                result.unlink(missing_ok=True)
                return payload
            await asyncio.sleep(self.poll)
        # Withdraw the job so a service that starts late does not fetch it.
        (jobs / f"{job_id}.job.json").unlink(missing_ok=True)
        raise FetchError("fetcher_unavailable")


def fetcher_heartbeat_age(exchange: Path) -> float | None:
    """Seconds since the fetch service last reported, or None if it never has."""
    try:
        return max(time.time() - float((exchange / HEARTBEAT).read_text()), 0.0)
    except (OSError, ValueError):
        return None
