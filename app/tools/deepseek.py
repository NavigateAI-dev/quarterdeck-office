"""DeepSeek's Fetch API as the office's fallback page reader.

The office reads pages itself first: through its own fetch service, which
holds no credentials, at no cost. Some pages refuse it (a bot wall, a script
challenge). For those, DeepSeek's Fetch API reads the page and returns it as
Markdown, at $0.75 per 1,000 pages actually returned (failures are free).

A page read this way is marked `read_via="deepseek"`, so the report shows its
weaker provenance, as for any page a third party read for the office. An
address inside the office is never sent: the same check as the office's own
reader runs first.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.browser.safety import url_allowed
from app.config import Settings
from app.tools.fetch import MAX_EXCERPT_CHARS, FetchError, Page

ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
TIMEOUT_SECONDS = 30.0
# DeepSeek allows 5 fetches a second per account; a task keeps well below it.
AT_ONCE = 3
READ_VIA = "deepseek"
CODES = {
    402: "fetch_out_of_credit",
    403: "fetch_refused",
    404: "fetch_no_content",
    429: "fetch_rate_limited",
}


class DeepSeekFetcher:
    def __init__(
        self,
        key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.key = key
        self.transport = transport
        self.timeout = timeout

    async def fetch(
        self, url: str, follow_redirects: int = 0, max_chars: int = MAX_EXCERPT_CHARS
    ) -> Page:
        if not url_allowed(url):
            raise FetchError("url_not_allowed")
        try:
            async with (
                asyncio.timeout(self.timeout),
                httpx.AsyncClient(transport=self.transport, trust_env=False) as client,
            ):
                response = await client.post(
                    ENDPOINT,
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"url": url},
                )
        except (httpx.TimeoutException, TimeoutError):
            raise FetchError("fetch_timeout") from None
        except httpx.TransportError:
            raise FetchError("fetch_unreachable") from None
        if response.status_code != 200:
            raise FetchError(CODES.get(response.status_code, "fetch_unavailable"))
        try:
            content = str(response.json().get("content") or "")
        except (ValueError, AttributeError):
            raise FetchError("fetch_malformed") from None
        if not content.strip():
            raise FetchError("fetch_no_content")
        return Page(url=url, text=content[:max_chars], read_via=READ_VIA)

    async def fetch_many(self, urls: list[str]) -> list[Page]:
        return await _many(self, urls)


class FallbackFetcher:
    """The office's own reader first; DeepSeek only for a page it cannot read."""

    def __init__(self, primary: Any, backup: Any) -> None:
        self.primary = primary
        self.backup = backup

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)

    async def fetch(self, url: str, **options: Any) -> Page:
        try:
            page: Page = await self.primary.fetch(url, **options)
            return page
        except FetchError as failed:
            try:
                backed: Page = await self.backup.fetch(url, **options)
                return backed
            except FetchError:
                raise failed from None

    async def fetch_many(self, urls: list[str]) -> list[Page]:
        return await _many(self, urls)


async def _many(fetcher: Any, urls: list[str]) -> list[Page]:
    """What is reachable, a few at a time; a page that fails is simply not evidence."""
    reads = asyncio.Semaphore(AT_ONCE)

    async def one(url: str) -> Page | None:
        async with reads:
            try:
                page: Page = await fetcher.fetch(url)
                return page
            except FetchError:
                return None

    return [page for page in await asyncio.gather(*map(one, urls)) if page is not None]


def with_deepseek_fallback(reader: Any, settings: Settings) -> Any:
    """The reader, with DeepSeek behind it when the owner turned that on."""
    if settings.fetch_fallback != "deepseek" or settings.deepseek_api_key is None:
        return reader
    return FallbackFetcher(reader, DeepSeekFetcher(settings.deepseek_api_key.get_secret_value()))
