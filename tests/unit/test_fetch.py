import asyncio
import json as _json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.tools.fetch import (
    FetchError,
    PageFetcher,
    assert_public_url,
    extract_text,
)


def fetcher(handler: object, **kwargs: object) -> PageFetcher:
    return PageFetcher(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://example.com", "scheme_not_allowed"),
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("ftp://example.com", "scheme_not_allowed"),
        ("https://127.0.0.1/admin", "non_public_address"),
        ("https://localhost/admin", "non_public_address"),
        ("https://169.254.169.254/latest/meta-data", "non_public_address"),
        ("https://10.0.0.5/internal", "non_public_address"),
        ("https://192.168.1.1/router", "non_public_address"),
    ],
)
def test_non_public_targets_are_refused(url: str, code: str) -> None:
    """URLs come from search results and model output: this is an SSRF boundary."""
    with pytest.raises(FetchError, match=code):
        assert_public_url(url)


def test_scripts_and_styles_are_discarded_not_parsed() -> None:
    html = "<html><script>steal()</script><style>p{}</style><p>Real text</p></html>"
    assert extract_text(html) == "Real text"


def test_markup_is_reduced_to_text() -> None:
    assert extract_text("<h1>Title</h1>\n<p>Body   here</p>") == "Title\nBody here"


def test_table_rows_stay_together_with_their_cells() -> None:
    """A pricing table must not run together, or tiers lose their prices."""
    html = (
        "<table><tr><th>Plan</th><th>Price</th><th>Credits</th></tr>"
        "<tr><td>Researcher</td><td>$30/mo</td><td>4,000</td></tr>"
        "<tr><td>Project</td><td>$100/mo</td><td>15,000</td></tr></table>"
    )
    assert extract_text(html).splitlines() == [
        "Plan | Price | Credits",
        "Researcher | $30/mo | 4,000",
        "Project | $100/mo | 15,000",
    ]


def test_list_items_and_card_blocks_are_separate_lines() -> None:
    html = (
        "<div><h3>Free</h3><p>$0</p><ul><li>1,000 credits</li><li>1 RPS</li></ul></div>"
        "<div><h3>Pro</h3><p>$5 per 1,000 requests</p></div>"
    )
    assert extract_text(html).splitlines() == [
        "Free",
        "$0",
        "- 1,000 credits",
        "- 1 RPS",
        "Pro",
        "$5 per 1,000 requests",
    ]


def test_entities_are_decoded_and_inline_markup_joins_words() -> None:
    assert extract_text("<p>Fast &amp; <b>cheap</b>&nbsp;plan</p>") == "Fast & cheap plan"


def test_scripts_hidden_in_tables_are_still_discarded() -> None:
    html = "<table><tr><td>Plan<script>alert(1)</script></td><td>$5</td></tr></table>"
    assert extract_text(html) == "Plan | $5"


@pytest.mark.asyncio
async def test_a_page_is_fetched_and_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<p>" + ("x" * 50_000) + "</p>", headers={"content-type": "text/html"}
        )

    async with fetcher(handler) as tool:
        page = await tool.fetch("https://example.com/doc")
    assert len(page.text) <= 4000


@pytest.mark.asyncio
async def test_redirects_are_not_followed() -> None:
    """A redirect would reach a host that was never validated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/admin"})

    async with fetcher(handler) as tool:
        with pytest.raises(FetchError, match="fetch_redirected"):
            await tool.fetch("https://example.com/doc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(404), "fetch_rejected"),
        (
            httpx.Response(200, content=b"\x00\x01", headers={"content-type": "application/zip"}),
            "unsupported_content_type",
        ),
        (httpx.Response(200, text="   ", headers={"content-type": "text/html"}), "empty_page"),
    ],
)
async def test_unusable_responses_are_refused(response: httpx.Response, code: str) -> None:
    async with fetcher(lambda request: response) as tool:
        with pytest.raises(FetchError, match=code):
            await tool.fetch("https://example.com/doc")


@pytest.mark.asyncio
async def test_failures_do_not_carry_page_bodies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal-secret-token")

    async with fetcher(handler) as tool:
        with pytest.raises(FetchError) as caught:
            await tool.fetch("https://example.com/doc")
    assert "internal-secret-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_an_unreachable_page_is_skipped_rather_than_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "good" in str(request.url):
            return httpx.Response(200, text="<p>Primary</p>", headers={"content-type": "text/html"})
        return httpx.Response(503)

    async with fetcher(handler) as tool:
        pages = await tool.fetch_many(
            ["https://example.com/bad", "https://example.com/good", "https://127.0.0.1/x"]
        )
    assert [page.text for page in pages] == ["Primary"]


@pytest.mark.asyncio
async def test_named_official_pages_may_follow_public_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/pricing"})
        return httpx.Response(200, text="<p>Plans</p>", headers={"content-type": "text/html"})

    async with fetcher(handler) as tool:
        page = await tool.fetch("https://example.com/pricing", follow_redirects=3)
    assert page.url == "https://www.example.com/pricing"
    assert page.retrieved_at.tzinfo is not None


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_refused_even_when_following() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/admin"})

    async with fetcher(handler) as tool:
        with pytest.raises(FetchError, match="non_public_address"):
            await tool.fetch("https://example.com/doc", follow_redirects=3)


@pytest.mark.asyncio
async def test_redirect_chains_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    async with fetcher(handler) as tool:
        with pytest.raises(FetchError, match="too_many_redirects"):
            await tool.fetch("https://example.com/doc", follow_redirects=2)


@pytest.mark.asyncio
async def test_a_longer_excerpt_can_be_requested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<p>" + ("y" * 50_000) + "</p>", headers={"content-type": "text/html"}
        )

    async with fetcher(handler) as tool:
        page = await tool.fetch("https://example.com/doc", max_chars=12_000)
    assert len(page.text) == 12_000


def test_links_are_collected_with_their_text_and_resolved() -> None:
    from app.tools.fetch import extract_links

    html = (
        '<a href="/documentation/rate-limits">Rate Limits</a>'
        '<a href="https://docs.tavily.com/credits#top">Credits</a>'
        '<a href="mailto:x@y.z">Mail</a><a href="javascript:alert(1)">x</a>'
        '<a href="/documentation/rate-limits">Rate Limits again</a>'
    )
    links = extract_links(html, "https://docs.tavily.com/welcome")
    assert links == [
        ("Rate Limits", "https://docs.tavily.com/documentation/rate-limits"),
        ("Credits", "https://docs.tavily.com/credits"),
    ]


@pytest.mark.parametrize(
    "a,b,same",
    [
        ("https://www.tavily.com/pricing", "https://docs.tavily.com/x", True),
        ("https://brave.com/search/api/", "https://api-dashboard.search.brave.com/d", True),
        ("https://tavily.com/", "https://evil.example/tavily.com", False),
        ("https://docs.example.co.uk/a", "https://other.co.uk/b", False),
    ],
)
def test_same_site_is_judged_by_registrable_domain(a: str, b: str, same: bool) -> None:
    from app.tools.fetch import same_site

    assert same_site(a, b) is same


@pytest.mark.asyncio
async def test_fetched_pages_carry_their_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='<p>Docs</p><a href="/rate-limits">Rate Limits</a>',
            headers={"content-type": "text/html"},
        )

    async with fetcher(handler) as tool:
        page = await tool.fetch("https://example.com/")
    assert page.links == (("Rate Limits", "https://example.com/rate-limits"),)


def test_markdown_links_in_plain_text_indexes_are_collected() -> None:
    from app.tools.fetch import extract_links

    text = (
        "# Tavily Docs\n"
        "- [Rate Limits](https://docs.tavily.com/documentation/rate-limits): per-plan RPM\n"
        "- [Credits](/documentation/api-credits)\n"
        "- [Unsafe](javascript:alert(1))\n"
    )
    assert extract_links(text, "https://docs.tavily.com/llms.txt") == [
        ("Rate Limits", "https://docs.tavily.com/documentation/rate-limits"),
        ("Credits", "https://docs.tavily.com/documentation/api-credits"),
    ]


def patient(handler: Callable[[httpx.Request], httpx.Response]) -> PageFetcher:
    """A fetcher that retries without making the test wait."""
    return PageFetcher(transport=httpx.MockTransport(handler), host_interval=0.0, retry_base=0.0)


@pytest.mark.asyncio
async def test_a_rate_limited_source_is_tried_again_not_abandoned() -> None:
    """The KYC re-run: wetten.overheid.nl answered 429 and the law went unread."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, text="<p>Artikel 3</p>", headers={"content-type": "text/html"})

    async with patient(handler) as tool:
        page = await tool.fetch("https://wetten.overheid.nl/BWBR0024282/")
    assert "Artikel 3" in page.text
    assert len(seen) == 2, "the refusal must be retried once, not accepted"


@pytest.mark.asyncio
async def test_an_accepted_but_not_ready_response_is_tried_again() -> None:
    """EUR-Lex answers 202 with an interstitial rather than the document."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(202, text="processing")
        return httpx.Response(200, text="<p>eIDAS</p>", headers={"content-type": "text/html"})

    async with patient(handler) as tool:
        page = await tool.fetch("https://eur-lex.europa.eu/eli/reg/2014/910/oj")
    assert "eIDAS" in page.text
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_a_source_that_keeps_refusing_says_what_it_answered() -> None:
    async with patient(lambda request: httpx.Response(429)) as tool:
        with pytest.raises(FetchError, match="fetch_rejected_429"):
            await tool.fetch("https://example.com/doc")


@pytest.mark.asyncio
async def test_requests_to_one_host_are_spaced_out() -> None:
    import time as clock

    handler = lambda request: httpx.Response(  # noqa: E731
        200, text="<p>ok</p>", headers={"content-type": "text/html"}
    )
    async with PageFetcher(transport=httpx.MockTransport(handler), host_interval=0.2) as tool:
        started = clock.monotonic()
        await tool.fetch("https://example.com/a")
        await tool.fetch("https://example.com/b")
        elapsed = clock.monotonic() - started
    assert elapsed >= 0.2, "two requests to one host must not be back to back"


@pytest.mark.asyncio
async def test_parallel_subtasks_fetch_an_official_source_once() -> None:
    """Four subtasks asking one host at once is how the office earned a 429."""
    import copy as copying

    from app.agents.researcher import ResearcherAgent

    calls: list[str] = []

    class CountingFetcher:
        async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 0) -> object:
            calls.append(url)
            from datetime import UTC, datetime

            from app.tools.fetch import Page

            return Page(url=url, text="Artikel 3", retrieved_at=datetime.now(UTC), links=())

    agent = ResearcherAgent.__new__(ResearcherAgent)
    agent.fetcher = CountingFetcher()  # type: ignore[assignment]
    agent.official_urls = ["https://wetten.overheid.nl/BWBR0024282/"]
    agent.official_cache = {}
    agent.official_failures = {}
    agent.official_lock = asyncio.Lock()
    agent.refused_sources = {}
    agent.last_aliases = {}
    agent.requested_by_final = {}

    workers = [copying.copy(agent) for _ in range(4)]
    await asyncio.gather(*(worker._official_pages() for worker in workers))
    assert len(calls) == 1, f"the same law was fetched {len(calls)} times"
    # Every subtask still received the page.
    assert all(worker.official_cache for worker in workers)


@pytest.mark.asyncio
async def test_the_office_says_who_it_is() -> None:
    """The Dutch legislation site answered 429 to an unidentified client.

    It returns the whole law to anything that names itself, so the office names
    itself rather than imitating a browser.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text="<p>Artikel 3</p>", headers={"content-type": "text/html"})

    async with patient(handler) as tool:
        await tool.fetch("https://wetten.overheid.nl/BWBR0024282/")
    agent = seen.get("user-agent", "")
    assert agent.startswith("Quarterdeck/"), agent
    assert "Mozilla" not in agent, "the office does not pretend to be a browser"


def page_of(text: str) -> object:
    from datetime import UTC, datetime

    from app.tools.fetch import Page

    return Page(url="https://example.com", text=text, retrieved_at=datetime.now(UTC), links=())


def test_the_wrong_document_is_refused_when_others_do_match() -> None:
    """A run asked about customer due diligence and was given the Criminal Code."""
    from app.agents.researcher import ResearcherAgent

    question = "Wwft cliëntenonderzoek EMI UBO verscherpt onderzoek eIDAS"
    pages = {
        "https://wetten.overheid.nl/BWBR0024282/": page_of(
            "Wet ter voorkoming van witwassen. Artikel 3 cliëntenonderzoek UBO. " * 60
        ),
        "https://wetten.overheid.nl/BWBR0001854/": page_of(
            "Wetboek van Strafrecht. Geen feit is strafbaar dan uit kracht. " * 60
        ),
    }
    refused = ResearcherAgent._off_topic(pages, question)
    assert refused == {"https://wetten.overheid.nl/BWBR0001854/"}


def test_a_question_in_another_language_refuses_nothing() -> None:
    """A Chinese question shares no terms with a Dutch law.

    Judging each page alone rejected the very source the plan got right.
    """
    from app.agents.researcher import ResearcherAgent

    question = "荷兰EMI在个人KYC与企业KYB/UBO流程中如何使用eIDAS电子身份与信任服务"
    pages = {
        "https://wetten.overheid.nl/BWBR0024282/": page_of(
            "Wet ter voorkoming van witwassen. Artikel 3 cliëntenonderzoek. " * 60
        ),
    }
    assert ResearcherAgent._off_topic(pages, question) == set()


def test_a_short_page_is_never_judged() -> None:
    """A landing page has too little text; refusing it would be the worse error."""
    from app.agents.researcher import ResearcherAgent

    pages = {
        "https://long.example": page_of("Wwft cliëntenonderzoek UBO eIDAS EMI. " * 60),
        "https://short.example": page_of("Onfido documentation portal."),
    }
    refused = ResearcherAgent._off_topic(pages, "Wwft cliëntenonderzoek EMI UBO eIDAS")
    assert "https://short.example" not in refused


@pytest.mark.asyncio
async def test_a_source_that_refuses_the_office_is_read_through_a_reader() -> None:
    """DNB answered 403 for days. A reader returns the page; provenance says so."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "r.jina.ai" in str(request.url):
            return httpx.Response(200, text="Title: Open Book Supervision\n\nThe text.")
        return httpx.Response(403)

    async with patient(handler) as tool:
        page = await tool.fetch("https://www.dnb.nl/en/")
    assert page.read_via == "reader"
    assert "Open Book Supervision" in page.text
    assert any("r.jina.ai" in url for url in seen)


@pytest.mark.asyncio
async def test_a_page_the_office_can_read_itself_never_leaves() -> None:
    """The fallback is a fallback: nothing readable is sent to a third party."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<p>ours</p>", headers={"content-type": "text/html"})

    async with patient(handler) as tool:
        page = await tool.fetch("https://example.com/doc")
    assert page.read_via == "direct"
    assert not any("r.jina.ai" in url for url in seen)


@pytest.mark.asyncio
async def test_a_missing_page_is_not_sent_to_a_reader() -> None:
    """A 404 is an answer. Only a refusal is worth asking someone else about."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    async with patient(handler) as tool:
        with pytest.raises(FetchError, match="fetch_rejected_404"):
            await tool.fetch("https://example.com/gone")
    assert not any("r.jina.ai" in url for url in seen)


@pytest.mark.asyncio
async def test_the_reader_can_be_turned_off_entirely() -> None:
    """Then nothing the office reads passes through anyone else."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "r.jina.ai" not in str(request.url), "the reader was called while off"
        return httpx.Response(403)

    tool = PageFetcher(
        transport=httpx.MockTransport(handler),
        host_interval=0.0,
        retry_base=0.0,
        reader_base=None,
    )
    async with tool:
        with pytest.raises(FetchError, match="fetch_rejected_403"):
            await tool.fetch("https://example.com/doc")


@pytest.mark.asyncio
async def test_a_reader_that_also_fails_leaves_the_refusal_standing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if "r.jina.ai" in str(request.url) else 403)

    async with patient(handler) as tool:
        with pytest.raises(FetchError, match="fetch_rejected_403"):
            await tool.fetch("https://example.com/doc")


def settings_for(provider: str) -> Any:
    from app.config import Settings

    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://office:secret@localhost/office",
        search_provider=provider,
        search_api_key="search-key",
    )


@pytest.mark.asyncio
async def test_exa_returns_the_page_with_each_result() -> None:
    """A provider that reads pages saves a fetch and reaches walled sources."""
    from app.tools.web import ExaSearchTool

    asked: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        asked["url"] = str(request.url)
        asked["key"] = request.headers.get("x-api-key")
        asked["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "eIDAS Regulation",
                        "url": "https://eur-lex.europa.eu/eli/reg/2014/910/oj",
                        "text": "Article 3 Definitions. For the purposes of this Regulation…",
                    }
                ]
            },
        )

    tool = ExaSearchTool(settings_for("exa"), transport=httpx.MockTransport(handler))
    async with tool:
        results = await tool.search("eIDAS article 3", limit=5)
    assert asked["key"] == "search-key"
    assert asked["body"]["numResults"] == 5
    assert asked["body"]["contents"]["text"], "the text is the point of using it"
    assert results[0].url.endswith("/oj")
    assert "Article 3 Definitions" in results[0].text


@pytest.mark.asyncio
async def test_tavily_returns_the_raw_page_and_asks_for_no_answer() -> None:
    """The office writes its own answer; a provider's summary is not evidence."""
    from app.tools.web import TavilySearchTool

    asked: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        asked["body"] = _json.loads(request.content)
        asked["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Wwft",
                        "url": "https://wetten.overheid.nl/BWBR0024282/",
                        "content": "A short description.",
                        "raw_content": "Artikel 3 Een instelling verricht cliëntenonderzoek.",
                    }
                ]
            },
        )

    tool = TavilySearchTool(settings_for("tavily"), transport=httpx.MockTransport(handler))
    async with tool:
        results = await tool.search("Wwft clientenonderzoek", limit=3)
    assert asked["auth"] == "Bearer search-key"
    assert asked["body"]["include_raw_content"] is True
    assert asked["body"]["include_answer"] is False, "a summary is not evidence"
    assert "Artikel 3" in results[0].text
    assert results[0].snippet == "A short description."


@pytest.mark.asyncio
async def test_a_content_provider_reports_a_refusal_rather_than_inventing() -> None:
    from app.tools.web import SearchError, TavilySearchTool

    for status, code in ((429, "search_rate_limited"), (500, "search_rejected")):
        tool = TavilySearchTool(
            settings_for("tavily"),
            transport=httpx.MockTransport(lambda request, s=status: httpx.Response(s)),
        )
        async with tool:
            with pytest.raises(SearchError, match=code):
                await tool.search("anything")


def test_the_office_can_be_pointed_at_any_of_the_providers() -> None:
    from app.tools.web import (
        BraveSearchTool,
        ExaSearchTool,
        StaticSearchTool,
        TavilySearchTool,
        build_search_tool,
    )

    for provider, expected in (
        ("brave", BraveSearchTool),
        ("exa", ExaSearchTool),
        ("tavily", TavilySearchTool),
        ("static", StaticSearchTool),
    ):
        assert isinstance(build_search_tool(settings_for(provider)), expected)


SITE = """<html><body>
<div id="cookie-banner"><p>We use cookies to improve your experience. Accept all cookies?</p>
<button>Accept</button><button>Reject</button></div>
<header class="site-header"><nav><ul>
<li><a href="/">Home</a></li><li><a href="/pricing">Pricing</a></li>
<li><a href="/docs">Docs</a></li>
<li><a href="/blog">Blog</a></li><li><a href="/careers">Careers</a></li>
<li><a href="/contact">Contact</a></li>
</ul></nav></header>
<main>
<h1>Pricing</h1>
<p>Every plan includes unlimited projects and email support. Prices are per month, billed
yearly.
The Pro plan adds single sign-on, audit logs and a dedicated account manager for teams that
need them.</p>
<table><tr><th>Plan</th><th>Price</th><th>Seats</th></tr>
<tr><td>Starter</td><td>$19</td><td>3</td></tr>
<tr><td>Pro</td><td>$59</td><td>10</td></tr>
<tr><td>Team</td><td>$129</td><td>25</td></tr></table>
<p>Annual billing saves two months compared with paying monthly. Taxes are added at checkout
where
required. You can change plans at any time and the difference is prorated to the day.</p>
</main>
<aside class="related"><h3>Related articles</h3><p>How we price our product, a story in five
parts.</p></aside>
<div class="newsletter"><p>Subscribe to our newsletter for product news and tips every
week.</p></div>
<footer><p>© 2026 Acme Inc. All rights reserved.</p><ul><li>Privacy</li><li>Terms</li>
<li>Cookies</li><li>Security</li><li>Status</li></ul></footer>
</body></html>"""


def test_a_page_keeps_its_content_and_drops_its_furniture() -> None:
    """WP2: the model reads the page, not the site around it."""
    text = extract_text(SITE)
    assert "Pricing" in text and "Pro | $59 | 10" in text and "prorated to the day" in text
    for furniture in (
        "cookies to improve",
        "Careers",
        "newsletter",
        "All rights reserved",
        "Related",
    ):
        assert furniture not in text, furniture


def test_cleaning_saves_a_good_part_of_the_text() -> None:
    from html.parser import HTMLParser

    class Everything(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            if data.strip():
                self.parts.append(data.strip())

    whole = Everything()
    whole.feed(SITE)
    before = len(" ".join(whole.parts))
    after = len(extract_text(SITE))
    assert after < before * 0.75, (before, after)


def test_without_a_main_region_the_page_minus_its_furniture_is_kept() -> None:
    html = (
        "<nav><a>Home</a><a>About</a></nav><h1>Opening hours</h1><p>Mon to Fri 9 to 17.</p>"
        "<footer>© Shop</footer>"
    )
    assert extract_text(html).splitlines() == ["Opening hours", "Mon to Fri 9 to 17."]


def test_a_thin_main_region_does_not_hide_the_page() -> None:
    html = (
        "<main><p>Loading…</p></main><section><h2>Menu</h2><p>"
        + "Pasta with tomato and basil. " * 30
        + "</p></section>"
    )
    text = extract_text(html)
    assert "Loading" in text and "Menu" in text and "Pasta with tomato" in text


def test_a_menu_repeated_on_the_page_is_read_once() -> None:
    html = (
        "<div><p>Home</p><p>Shop</p></div><div><p>Home</p><p>Shop</p><p>Real words here.</p></div>"
    )
    assert extract_text(html).splitlines() == ["Home", "Shop", "Real words here."]
