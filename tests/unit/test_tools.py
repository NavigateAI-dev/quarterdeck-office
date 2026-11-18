import httpx
import pytest

from app.config import Settings
from app.tools.web import (
    MAX_QUERY_LENGTH,
    BraveSearchTool,
    SearchError,
    SearchResult,
    StaticSearchTool,
)


def config(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/x",
        **overrides,
    )


def brave(handler: object, **overrides: object) -> BraveSearchTool:
    settings = config(search_provider="brave", search_api_key="search-fixture-key", **overrides)
    return BraveSearchTool(settings, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_static_tool_caps_results() -> None:
    results = [SearchResult(f"t{i}", f"https://example.com/{i}", "s") for i in range(5)]
    assert len(await StaticSearchTool(results).search("query", limit=2)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,limit,code",
    [
        ("  ", 10, "empty_query"),
        ("x" * (MAX_QUERY_LENGTH + 1), 10, "query_too_long"),
        ("q", 0, "invalid_limit"),
    ],
)
async def test_query_bounds_are_enforced(query: str, limit: int, code: str) -> None:
    with pytest.raises(SearchError, match=code):
        await StaticSearchTool([]).search(query, limit=limit)


@pytest.mark.asyncio
async def test_brave_adapter_maps_results_and_sends_credential() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Subscription-Token", "")
        seen["query"] = request.url.params.get("q", "")
        seen["path"] = request.url.path
        body = {
            "web": {"results": [{"title": "t", "url": "https://example.com", "description": "d"}]}
        }
        return httpx.Response(200, json=body)

    async with brave(handler) as tool:
        results = await tool.search("market demand")
    assert results == [SearchResult("t", "https://example.com", "d")]
    assert seen == {
        "token": "search-fixture-key",
        "query": "market demand",
        "path": "/res/v1/web/search",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(503), "search_rejected"),
        (httpx.Response(301, headers={"location": "https://example.com"}), "search_redirected"),
        (httpx.Response(200, text="not json"), "malformed_search_response"),
    ],
)
async def test_provider_failures_are_sanitized(response: httpx.Response, code: str) -> None:
    async with brave(lambda request: response) as tool:
        with pytest.raises(SearchError, match=code):
            await tool.search("query")


@pytest.mark.asyncio
async def test_transport_failure_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with brave(handler) as tool:
        with pytest.raises(SearchError, match="search_unreachable"):
            await tool.search("query")


def test_brave_requires_a_credential() -> None:
    with pytest.raises(SearchError, match="search_not_configured"):
        BraveSearchTool(config())


def test_non_static_provider_requires_a_credential() -> None:
    with pytest.raises(ValueError, match="SEARCH_API_KEY is required"):
        config(search_provider="brave")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="SEARCH_PROVIDER"):
        config(search_provider="unknown")


def test_factory_selects_the_configured_provider() -> None:
    from app.tools.web import build_search_tool

    assert isinstance(build_search_tool(config()), StaticSearchTool)
    tool = build_search_tool(
        config(search_provider="brave", search_api_key="search-fixture-key"),
    )
    assert isinstance(tool, BraveSearchTool)


@pytest.mark.asyncio
async def test_rate_limited_search_retries_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The free plan allows one query per second; research runs repeatedly."""
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.tools.web.asyncio.sleep", no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset": "1, 1157711"})
        body = {
            "web": {"results": [{"title": "t", "url": "https://example.com", "description": "d"}]}
        }
        return httpx.Response(200, json=body)

    async with brave(handler) as tool:
        results = await tool.search("query")
    assert len(results) == 1
    assert calls == 2
    assert slept == [1.0]


@pytest.mark.asyncio
async def test_rate_limit_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("app.tools.web.asyncio.sleep", no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    async with brave(handler) as tool:
        with pytest.raises(SearchError, match="search_rate_limited"):
            await tool.search("query")
    assert calls == 4


@pytest.mark.asyncio
async def test_retry_wait_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.tools.web.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"x-ratelimit-reset": "9999"})

    async with brave(handler) as tool:
        with pytest.raises(SearchError):
            await tool.search("query")
    assert slept == [5.0, 5.0, 5.0]
