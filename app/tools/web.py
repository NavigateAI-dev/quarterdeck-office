import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings

MAX_QUERY_LENGTH = 512
MAX_RESULTS = 25
MAX_RATE_LIMIT_RETRIES = 3
MAX_RATE_LIMIT_WAIT = 5.0


class SearchError(Exception):
    """Sanitized search failure. Never carries provider response bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


UNKNOWN_SOURCE_TYPE = "unknown"
# How much of a returned page the office keeps. A provider will hand back a
# whole document; the run's budget is the reason not to take all of it.
MAX_RESULT_TEXT = 40_000
SNIPPET_CHARS = 400


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    # "unknown" when the provider cannot tell. A provider that does know
    # asserts it, and the researcher may not contradict that assertion.
    source_type: str = UNKNOWN_SOURCE_TYPE
    # The page itself, when the provider returned it. A provider that reads
    # pages saves the office a fetch, and reaches sources that refuse it.
    text: str = ""


class SearchTool(Protocol):
    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]: ...


def bounded_query(query: str, limit: int) -> tuple[str, int]:
    """Validate query and limit against fixed tool bounds."""
    text = query.strip()
    if not text:
        raise SearchError("empty_query")
    if len(text) > MAX_QUERY_LENGTH:
        raise SearchError("query_too_long")
    if limit < 1:
        raise SearchError("invalid_limit")
    return text, min(limit, MAX_RESULTS)


class StaticSearchTool:
    """Deterministic adapter for tests and local development."""

    def __init__(self, results: list[SearchResult]) -> None:
        self.results = list(results)

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        _, capped = bounded_query(query, limit)
        return self.results[:capped]


class _BraveResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    description: str = ""


class _BraveWeb(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[_BraveResult] = Field(default_factory=list)


class _BraveResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    web: _BraveWeb = Field(default_factory=_BraveWeb)


class BraveSearchTool:
    """Brave Search adapter: GET {base_url} with the subscription token header.

    Bounded by a total timeout and a maximum result count. Provider errors are
    reported as codes, never as response bodies.
    """

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if settings.search_api_key is None:
            raise SearchError("search_not_configured")
        self.timeout = settings.search_timeout_seconds
        # Request the full URL per call. An httpx base_url is normalized to end
        # with "/", and the trailing slash redirects to an HTML page.
        self.url = str(settings.search_base_url)
        self.client = httpx.AsyncClient(
            headers={
                "X-Subscription-Token": settings.search_api_key.get_secret_value(),
                "Accept": "application/json",
            },
            timeout=self.timeout,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> "BraveSearchTool":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.aclose()

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        text, capped = bounded_query(query, limit)
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            response = await self._request(text, capped)
            if response.status_code != 429:
                break
            # The free plan permits one query per second, and research runs
            # repeatedly within a revision loop. Wait for the bucket rather
            # than failing the run over a transient limit.
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise SearchError("search_rate_limited")
            await asyncio.sleep(self._retry_after(response))
        if response.is_redirect:
            raise SearchError("search_redirected")
        if response.status_code != 200:
            raise SearchError("search_rejected")
        try:
            parsed = _BraveResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise SearchError("malformed_search_response") from None
        return [
            SearchResult(
                title=item.title,
                url=item.url,
                snippet=item.description,
                source_type=UNKNOWN_SOURCE_TYPE,
            )
            for item in parsed.web.results[:capped]
        ]

    async def _request(self, text: str, capped: int) -> httpx.Response:
        try:
            async with asyncio.timeout(self.timeout):
                return await self.client.get(self.url, params={"q": text, "count": capped})
        except (httpx.TimeoutException, TimeoutError):
            raise SearchError("search_timeout") from None
        except httpx.TransportError:
            raise SearchError("search_unreachable") from None

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        """Seconds to wait, taken from the provider's own reset header."""
        raw = response.headers.get("x-ratelimit-reset", "")
        first = raw.split(",")[0].strip()
        try:
            return min(max(float(first), 1.0), MAX_RATE_LIMIT_WAIT)
        except ValueError:
            return 1.0


class _ContentSearchTool:
    """Search providers that return the page, not only a pointer to it.

    Brave answers with a title, an address and a description, so the office
    must then fetch every page itself — and that fetch is where a regulator's
    bot wall or a JavaScript challenge stops it. A provider that returns the
    text has already done the reading.
    """

    endpoint = ""
    reads_pages = True

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = self._key(settings)
        self.timeout = settings.search_timeout_seconds
        self.client = httpx.AsyncClient(
            headers=self._headers(key),
            timeout=self.timeout,
            transport=transport,
            trust_env=False,
        )

    @staticmethod
    def _key(settings: Settings) -> str:
        return settings.search_api_key.get_secret_value() if settings.search_api_key else ""

    def _headers(self, key: str) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, query: str, limit: int) -> dict[str, Any]:
        raise NotImplementedError

    def _results(self, payload: dict[str, Any], limit: int) -> list[SearchResult]:
        raise NotImplementedError

    async def __aenter__(self) -> "_ContentSearchTool":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.aclose()

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        text, capped = bounded_query(query, limit)
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.post(self.endpoint, json=self._body(text, capped))
        except (httpx.TimeoutException, TimeoutError):
            raise SearchError("search_timeout") from None
        except httpx.TransportError:
            raise SearchError("search_unreachable") from None
        if response.status_code == 429:
            raise SearchError("search_rate_limited")
        if response.status_code != 200:
            raise SearchError("search_rejected")
        try:
            payload = response.json()
        except ValueError:
            raise SearchError("malformed_search_response") from None
        if not isinstance(payload, dict):
            raise SearchError("malformed_search_response")
        return self._results(payload, capped)


class ExaSearchTool(_ContentSearchTool):
    """Exa: neural search that returns the page text with each result."""

    endpoint = "https://api.exa.ai/search"

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _body(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "query": query,
            "numResults": limit,
            # The text is the point: it is what the office would otherwise have
            # to fetch for itself.
            "contents": {"text": {"maxCharacters": MAX_RESULT_TEXT}},
        }

    def _results(self, payload: dict[str, Any], limit: int) -> list[SearchResult]:
        found = []
        for item in (payload.get("results") or [])[:limit]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            body = str(item.get("text") or "")
            found.append(
                SearchResult(
                    title=str(item.get("title") or item["url"]),
                    url=str(item["url"]),
                    snippet=body[:SNIPPET_CHARS] or str(item.get("summary") or ""),
                    text=body[:MAX_RESULT_TEXT],
                )
            )
        return found


class TavilySearchTool(_ContentSearchTool):
    """Tavily: search for agents, with the raw page attached."""

    endpoint = "https://api.tavily.com/search"

    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "query": query,
            "max_results": limit,
            "search_depth": "advanced",
            "include_raw_content": True,
            # The office writes its own answer from the evidence; a provider's
            # summary is not evidence and is never asked for.
            "include_answer": False,
        }

    def _results(self, payload: dict[str, Any], limit: int) -> list[SearchResult]:
        found = []
        for item in (payload.get("results") or [])[:limit]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            body = str(item.get("raw_content") or "")
            found.append(
                SearchResult(
                    title=str(item.get("title") or item["url"]),
                    url=str(item["url"]),
                    snippet=str(item.get("content") or body[:SNIPPET_CHARS]),
                    text=body[:MAX_RESULT_TEXT],
                )
            )
        return found


class DeepSeekSearchTool(_ContentSearchTool):
    """DeepSeek's Search API: its own engine, with page content.

    A result's `summary` is written for models (600–800 characters); it stands
    as the snippet, and the page content as the text the office would else
    have to fetch. `site:`, `after:` and `lang:` work inside the query.
    """

    endpoint = "https://api.deepseek.com/v1/tools/search"

    @staticmethod
    def _key(settings: Settings) -> str:
        key = settings.search_api_key or settings.deepseek_api_key
        return key.get_secret_value() if key else ""

    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, query: str, limit: int) -> dict[str, Any]:
        return {"query": query, "count": limit, "need_content": True}

    def _results(self, payload: dict[str, Any], limit: int) -> list[SearchResult]:
        found = []
        for item in (payload.get("results") or [])[:limit]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            body = str(item.get("content") or "")
            found.append(
                SearchResult(
                    title=str(item.get("title") or item["url"]),
                    url=str(item["url"]),
                    snippet=str(item.get("summary") or item.get("snippet") or body[:SNIPPET_CHARS]),
                    text=body[:MAX_RESULT_TEXT],
                )
            )
        return found


def build_search_tool(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> SearchTool:
    """Select the configured search provider.

    The 'static' provider returns no results and is intended for tests and local
    development; research cannot be accepted without real sources.
    """
    if settings.search_provider == "brave":
        return BraveSearchTool(settings, transport=transport)
    if settings.search_provider == "exa":
        return ExaSearchTool(settings, transport=transport)
    if settings.search_provider == "tavily":
        return TavilySearchTool(settings, transport=transport)
    if settings.search_provider == "deepseek":
        return DeepSeekSearchTool(settings, transport=transport)
    return StaticSearchTool([])
