"""DeepSeek's Search and Fetch APIs as office tools."""

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.tools.deepseek import DeepSeekFetcher, FallbackFetcher, with_deepseek_fallback
from app.tools.fetch import FetchError, Page
from app.tools.web import DeepSeekSearchTool, SearchError, build_search_tool

KEY = "sk-deepseek-test-key-0123456789"


def settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://office:secret@localhost/office",
        **overrides,
    )


def recording(status: int, body: Any, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


RESULTS = {
    "results": [
        {
            "rerank_score": 2.4,
            "title": "BTW-tarieven",
            "url": "https://www.belastingdienst.nl/btw",
            "snippet": "Het algemene tarief is 21%.",
            "summary": "In Nederland is het algemene btw-tarief 21%, het verlaagde 9%.",
            "content": "# BTW\n\nHet algemene tarief is 21%.",
            "date": "2026-01-01T00:00:00+00:00",
            "authority": 4,
        },
        {"title": "no address"},
    ]
}


async def test_search_asks_deepseek_with_the_key_and_reads_the_pages() -> None:
    seen: list[httpx.Request] = []
    tool = DeepSeekSearchTool(
        settings(search_provider="deepseek", search_api_key=KEY),
        transport=recording(200, RESULTS, seen),
    )

    found = await tool.search("btw tarief nederland", limit=5)

    assert str(seen[0].url) == "https://api.deepseek.com/v1/tools/search"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(seen[0].content) == {
        "query": "btw tarief nederland",
        "count": 5,
        "need_content": True,
    }
    [result] = found
    assert result.url == "https://www.belastingdienst.nl/btw"
    assert result.snippet.startswith("In Nederland")
    assert result.text.startswith("# BTW")


async def test_search_uses_the_deepseek_key_when_search_has_none_of_its_own() -> None:
    seen: list[httpx.Request] = []
    config = settings(search_provider="deepseek", deepseek_api_key=KEY)

    await DeepSeekSearchTool(config, transport=recording(200, {"results": []}, seen)).search("q")

    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(
    ("status", "code"), [(429, "search_rate_limited"), (402, "search_rejected")]
)
async def test_search_failures_are_codes_never_bodies(status: int, code: str) -> None:
    tool = DeepSeekSearchTool(
        settings(search_provider="deepseek", search_api_key=KEY),
        transport=recording(status, {"error": {"message": KEY}}, []),
    )

    with pytest.raises(SearchError, match=code):
        await tool.search("q")


def test_deepseek_is_a_search_provider_the_office_can_choose() -> None:
    tool = build_search_tool(settings(search_provider="deepseek", search_api_key=KEY))

    assert isinstance(tool, DeepSeekSearchTool)


def test_choosing_deepseek_search_needs_a_key() -> None:
    with pytest.raises(ValueError, match="SEARCH_API_KEY"):
        settings(search_provider="deepseek")


async def test_fetch_returns_the_page_as_markdown_read_via_deepseek() -> None:
    seen: list[httpx.Request] = []
    fetcher = DeepSeekFetcher(
        KEY, transport=recording(200, {"title": "BTW", "content": "# BTW\n\n21%"}, seen)
    )

    page = await fetcher.fetch("https://www.belastingdienst.nl/btw", max_chars=5)

    assert str(seen[0].url) == "https://api.deepseek.com/v1/tools/fetch"
    assert json.loads(seen[0].content) == {"url": "https://www.belastingdienst.nl/btw"}
    assert (page.url, page.text, page.read_via) == (
        "https://www.belastingdienst.nl/btw",
        "# BTW",
        "deepseek",
    )


async def test_an_address_inside_the_office_is_never_sent_to_deepseek() -> None:
    seen: list[httpx.Request] = []
    fetcher = DeepSeekFetcher(KEY, transport=recording(200, {"content": "x"}, seen))

    with pytest.raises(FetchError, match="url_not_allowed"):
        await fetcher.fetch("http://localhost:8000/admin")

    assert seen == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (402, "fetch_out_of_credit"),
        (403, "fetch_refused"),
        (404, "fetch_no_content"),
        (429, "fetch_rate_limited"),
        (500, "fetch_unavailable"),
    ],
)
async def test_fetch_failures_are_codes(status: int, code: str) -> None:
    fetcher = DeepSeekFetcher(KEY, transport=recording(status, {"error": {"message": KEY}}, []))

    with pytest.raises(FetchError, match=code):
        await fetcher.fetch("https://example.org/")


async def test_an_empty_page_is_no_content() -> None:
    fetcher = DeepSeekFetcher(KEY, transport=recording(200, {"title": "", "content": "  "}, []))

    with pytest.raises(FetchError, match="fetch_no_content"):
        await fetcher.fetch("https://example.org/")


class Reader:
    def __init__(self, page: Page | FetchError) -> None:
        self.page = page
        self.asked: list[str] = []

    async def fetch(self, url: str, **options: Any) -> Page:
        self.asked.append(url)
        if isinstance(self.page, FetchError):
            raise self.page
        return self.page


async def test_deepseek_is_asked_only_when_the_office_cannot_read_the_page() -> None:
    office = Reader(Page(url="https://a.org/", text="office read it"))
    deepseek = Reader(Page(url="https://a.org/", text="deepseek read it", read_via="deepseek"))

    page = await FallbackFetcher(office, deepseek).fetch("https://a.org/")

    assert page.text == "office read it" and deepseek.asked == []


async def test_a_page_the_office_cannot_read_is_read_through_deepseek() -> None:
    office = Reader(FetchError("blocked_by_site"))
    deepseek = Reader(Page(url="https://a.org/", text="deepseek read it", read_via="deepseek"))

    pages = await FallbackFetcher(office, deepseek).fetch_many(["https://a.org/"])

    assert [p.read_via for p in pages] == ["deepseek"]


async def test_when_neither_can_read_it_the_office_error_stands() -> None:
    fallback = FallbackFetcher(
        Reader(FetchError("blocked_by_site")), Reader(FetchError("fetch_no_content"))
    )

    with pytest.raises(FetchError, match="blocked_by_site"):
        await fallback.fetch("https://a.org/")


def test_the_fallback_is_added_only_when_the_owner_turned_it_on() -> None:
    office = Reader(Page(url="u", text="t"))

    assert with_deepseek_fallback(office, settings()) is office
    on = with_deepseek_fallback(office, settings(fetch_fallback="deepseek", deepseek_api_key=KEY))
    assert isinstance(on, FallbackFetcher)


def test_turning_the_fallback_on_needs_a_key() -> None:
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        settings(fetch_fallback="deepseek")
