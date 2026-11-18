import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

MAX_PAGE_BYTES = 1_000_000
# A site answering 429 is asking the office to slow down; 202 and 503 mean
# "not ready". None of them mean the page does not exist.
RETRY_STATUS = frozenset({202, 429, 503})
RETRY_ATTEMPTS = 3
RETRY_BASE = 2.0
MAX_BACKOFF = 20.0
# Minimum gap between two requests to one host.
HOST_INTERVAL = 1.5
# Who is asking. Sites reasonably refuse an unidentified library default: the
# Dutch legislation site answers 429 to python-httpx and returns the whole law
# to anything that names itself. This says what the office is, honestly, rather
# than imitating a browser.
USER_AGENT = "Quarterdeck/1.0 (private research assistant; single owner; contact: local operator)"
# A source that refuses the office outright. A reader service reaches several
# of these — a regulator behind a bot wall, a register behind a JavaScript
# challenge — and the office would otherwise record them as simply unreadable.
REFUSING_STATUS = frozenset({202, 401, 403, 406, 429, 451, 503})
READER_BASE = "https://r.jina.ai/"
READER_TIMEOUT = 45.0
READER = "reader"
DIRECT = "direct"
MAX_EXCERPT_CHARS = 4000
ALLOWED_SCHEMES = frozenset({"https"})
ALLOWED_CONTENT = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "text/markdown",
    "text/x-markdown",
)

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


class FetchError(Exception):
    """Sanitized fetch failure. Never carries page bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Page:
    url: str
    text: str
    # When the page was read; becomes the source's access date.
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # (anchor text, absolute https URL) pairs, for following official docs.
    links: tuple[tuple[str, str], ...] = ()
    # How it was read: the office itself, or a reader service that fetched it
    # on the office's behalf. Carried into the report, because a page read
    # through a third party is a weaker provenance than one read directly.
    read_via: str = "direct"


BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "hr",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "blockquote",
        "pre",
        "figure",
        "form",
    }
)
SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})


# A page's furniture, not its content: menus, footers, cookie notices,
# share bars. Kept conservative: "menu" is a restaurant's content, so only names
# that are furniture on every site are matched.
FURNITURE_TAGS = frozenset(
    {"nav", "footer", "aside", "form", "dialog", "button", "select", "iframe"}
)
FURNITURE_ROLES = frozenset(
    {
        "navigation",
        "banner",
        "contentinfo",
        "complementary",
        "search",
        "dialog",
        "alertdialog",
        "menubar",
    }
)
FURNITURE_NAMES = re.compile(
    r"(?:^|[\s_-])(?:cookie|cookies|consent|gdpr|newsletter|subscribe|social|share|sharing|"
    r"breadcrumbs?|skip-?link|navbar|site-?header|site-?footer|advert|advertisement|ads?|"
    r"sponsor(?:ed)?|promo|popup|modal|paywall)(?:$|[\s_-])",
    re.IGNORECASE,
)
MAIN_TAGS = frozenset({"main", "article"})
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
# The main region is used alone only when it holds enough of the page to be it.
MAIN_MIN_CHARS = 400
# A short line seen before is a menu entry repeated, not new content.
REPEAT_CHARS = 80


class _TextExtractor(HTMLParser):
    """Keeps a page's reading structure: one line per block, list item or table
    row, with table cells joined by ' | ', so a pricing row stays one unit.

    Furniture (navigation, footers, cookie notices, share bars) is left out,
    and when the page marks its main content (main, article, role=main) and
    that holds enough text, only that is kept: a model paid per token
    should read the page, not the site around it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[tuple[str, bool]] = []
        self.line: list[str] = []
        self.cells: list[str] | None = None
        self.skipping = 0
        # Open elements: (tag, is furniture, is main content).
        self.open: list[tuple[str, bool, bool]] = []
        self.furniture = 0
        self.main = 0

    def _flush(self) -> None:
        text = _WHITESPACE.sub(" ", "".join(self.line)).strip()
        if text and not self.furniture:
            self.lines.append((text, self.main > 0))
        self.line = []

    @staticmethod
    def _is_furniture(tag: str, attrs: dict[str, str]) -> bool:
        if tag in FURNITURE_TAGS:
            return True
        if attrs.get("role", "").lower() in FURNITURE_ROLES:
            return True
        if attrs.get("aria-hidden", "").lower() == "true" or "hidden" in attrs:
            return True
        names = f"{attrs.get('id', '')} {attrs.get('class', '')}"
        return bool(names.strip()) and bool(FURNITURE_NAMES.search(names))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping += 1
        elif tag == "tr":
            self._flush()
            self.cells = []
        elif tag in ("td", "th") and self.cells is not None:
            self._end_cell()
        elif tag == "li":
            self._flush()
            self.line.append("- ")
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag in VOID_TAGS or tag in SKIPPED_TAGS:
            return
        given = {name.lower(): (value or "") for name, value in attrs}
        furniture = self._is_furniture(tag, given)
        main = tag in MAIN_TAGS or given.get("role", "").lower() == "main"
        if furniture or main:
            self._flush()
        self.open.append((tag, furniture, main))
        self.furniture += furniture
        self.main += main

    def _end_cell(self) -> None:
        if self.cells is None:
            return
        text = _WHITESPACE.sub(" ", "".join(self.line)).strip()
        if text:
            self.cells.append(text)
        self.line = []

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr" and self.cells is not None:
            self._end_cell()
            if self.cells and not self.furniture:
                self.lines.append((" | ".join(self.cells), self.main > 0))
            self.cells = None
        elif tag == "li" or tag in BLOCK_TAGS:
            self._flush()
        # Close up to the matching open element; HTML often leaves some unclosed.
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index][0] == tag:
                if any(furniture or main for _, furniture, main in self.open[index:]):
                    self._flush()
                for _, furniture, main in self.open[index:]:
                    self.furniture -= furniture
                    self.main -= main
                del self.open[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.line.append(data)

    def text(self) -> str:
        self._flush()
        chosen = [text for text, in_main in self.lines if in_main]
        if sum(len(text) for text in chosen) < MAIN_MIN_CHARS:
            chosen = [text for text, _ in self.lines]
        kept: list[str] = []
        seen: set[str] = set()
        for text in chosen:
            if len(text) <= REPEAT_CHARS:
                if text in seen:
                    continue
                seen.add(text)
            kept.append(text)
        return "\n".join(kept)


MAX_LINKS = 200
SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})


class _LinkExtractor(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = httpx.URL(base)
        self.links: list[tuple[str, str]] = []
        self.seen: set[str] = set()
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.href is None:
            return
        href, self.href = self.href, None
        try:
            url = self.base.join(href).copy_with(fragment=None)
        except (httpx.InvalidURL, ValueError):
            return
        text = _WHITESPACE.sub(" ", "".join(self.text)).strip()
        if url.scheme != "https" or str(url) in self.seen or len(self.links) >= MAX_LINKS:
            return
        self.seen.add(str(url))
        self.links.append((text, str(url)))


_MARKDOWN_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(([^)\s]{1,2000})\)")


def extract_links(html: str, base: str) -> list[tuple[str, str]]:
    """https links with their text, resolved against the page, deduplicated.

    Reads HTML anchors and Markdown links, so plain-text documentation indexes
    such as llms.txt yield their links too.
    """
    parser = _LinkExtractor(base)
    parser.feed(_SCRIPT_OR_STYLE.sub(" ", html))
    parser.close()
    for text, href in _MARKDOWN_LINK.findall(html):
        if len(parser.links) >= MAX_LINKS:
            break
        try:
            url = parser.base.join(href).copy_with(fragment=None)
        except (httpx.InvalidURL, ValueError):
            continue
        if url.scheme == "https" and str(url) not in parser.seen:
            parser.seen.add(str(url))
            parser.links.append((text.strip(), str(url)))
    return parser.links


def _site(url: str) -> str:
    labels = (httpx.URL(url).host or "").lower().split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(first: str, second: str) -> bool:
    """Whether two URLs share a registrable domain (docs.x.com and www.x.com do)."""
    try:
        return bool(_site(first)) and _site(first) == _site(second)
    except (httpx.InvalidURL, ValueError):
        return False


def extract_text(html: str) -> str:
    """Reduce markup to readable text, keeping line and table-row structure.

    Page content is data, never markup to render or code to run: scripts and
    styles are discarded rather than parsed.
    """
    parser = _TextExtractor()
    parser.feed(_SCRIPT_OR_STYLE.sub(" ", html))
    parser.close()
    return parser.text()


def assert_public_url(url: str) -> str:
    """Refuse anything that is not a public HTTPS address.

    The URLs come from search results and model output, so this is an SSRF
    boundary: private, loopback, and link-local addresses are unreachable, and
    the host is resolved here rather than trusted by name.
    """
    parsed = httpx.URL(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchError("scheme_not_allowed")
    host = parsed.host
    if not host:
        raise FetchError("missing_host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError("host_unresolvable") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise FetchError("non_public_address")
    return url


class PageFetcher:
    """Reads primary source pages within fixed bounds.

    Redirects are not followed: a redirect would revalidate to a different
    host than the one that was checked.
    """

    def __init__(
        self,
        timeout: float = 15.0,
        max_bytes: int = MAX_PAGE_BYTES,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        host_interval: float = HOST_INTERVAL,
        retry_base: float = RETRY_BASE,
        reader_base: str | None = READER_BASE,
        video_info: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.host_interval = host_interval
        self.retry_base = retry_base
        # None turns the fallback off entirely: nothing the office reads then
        # passes through anyone else.
        self.reader_base = reader_base
        # How a video's details are read: yt-dlp, unless a test says otherwise.
        if video_info is None:
            from app.tools.platforms import youtube_info

            video_info = youtube_info
        self.video_info = video_info
        # When each host was last asked, so the office does not rate-limit
        # itself out of the very sources its plan named.
        self._last_call: dict[str, float] = {}
        self.client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={
                "Accept": "text/html,text/plain;q=0.9,application/rss+xml;q=0.8,"
                "application/atom+xml;q=0.8",
                "User-Agent": USER_AGENT,
            },
        )

    async def __aenter__(self) -> "PageFetcher":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.aclose()

    async def _be_polite(self, url: str) -> None:
        """Leave a gap between two requests to the same host."""
        host = httpx.URL(url).host or ""
        last = self._last_call.get(host)
        now = time.monotonic()
        if last is not None and now - last < self.host_interval:
            await asyncio.sleep(self.host_interval - (now - last))
        self._last_call[host] = time.monotonic()

    def _backoff(self, response: httpx.Response, attempt: int) -> float:
        """Wait as long as the site asks, or a little longer each time."""
        asked = response.headers.get("retry-after", "")
        if asked.isdigit():
            return min(float(asked), MAX_BACKOFF)
        return float(min(self.retry_base * (2 ** (attempt - 1)), MAX_BACKOFF))

    async def _through_reader(self, url: str) -> Page | None:
        """Ask a reader service for a page this office was refused.

        Only after the office has tried and been refused, so a page it can read
        itself never leaves. The page comes back marked as read through a
        reader, and that marking travels into the report: a reader is a
        weaker provenance than a direct read, not a replacement for one.
        """
        if not self.reader_base:
            return None
        try:
            async with asyncio.timeout(READER_TIMEOUT):
                response = await self.client.get(f"{self.reader_base}{url}", timeout=READER_TIMEOUT)
        except (httpx.TimeoutException, TimeoutError, httpx.TransportError):
            return None
        if response.status_code != 200:
            return None
        body = response.content[: self.max_bytes].decode("utf-8", errors="replace")
        # A reader returns text, not a document to parse.
        text = " ".join(body.split()) if "<" not in body[:200] else extract_text(body)
        if not text:
            return None
        return Page(url=url, text=text, read_via=READER)

    async def fetch(
        self, url: str, follow_redirects: int = 0, max_chars: int = MAX_EXCERPT_CHARS
    ) -> Page:
        """Fetch one page. Redirects are refused unless the caller allows a
        bounded number, and every hop is revalidated as a public address."""
        # A platform that can say more than its page, without signing in, is
        # asked first; one that cannot answer leaves it to the read below.
        from app.tools.platforms import (
            FEED_TYPES,
            PlatformError,
            feed_page,
            platform_for,
            read_platform,
        )

        if platform_for(url) is not None:
            assert_public_url(url)
            try:
                return await read_platform(url, self.client, max_chars, self.video_info)
            except PlatformError:
                pass
        hops = 0
        attempts = 0
        while True:
            assert_public_url(url)
            await self._be_polite(url)
            try:
                async with asyncio.timeout(self.timeout):
                    response = await self.client.get(url)
            except (httpx.TimeoutException, TimeoutError):
                raise FetchError("fetch_timeout") from None
            except httpx.TransportError:
                raise FetchError("fetch_unreachable") from None
            if response.status_code in RETRY_STATUS and attempts < RETRY_ATTEMPTS:
                # 429 is the office asking too fast, and 202 is a site saying
                # "not yet". Both used to end the same way as a refusal, which
                # is how a plan could name a law and the run never read it.
                attempts += 1
                await asyncio.sleep(self._backoff(response, attempts))
                continue
            if not response.is_redirect:
                break
            if follow_redirects == 0:
                raise FetchError("fetch_redirected")
            hops += 1
            if hops > follow_redirects:
                raise FetchError("too_many_redirects")
            location = response.headers.get("location", "")
            url = str(httpx.URL(url).join(location))
        if response.status_code != 200:
            if response.status_code in REFUSING_STATUS:
                read = await self._through_reader(url)
                if read is not None:
                    return Page(
                        url=read.url,
                        text=read.text[:max_chars],
                        retrieved_at=read.retrieved_at,
                        links=read.links,
                        read_via=READER,
                    )
            raise FetchError(f"fetch_rejected_{response.status_code}")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type in FEED_TYPES or (
            not content_type and url.lower().endswith((".rss", ".atom", "/feed", ".xml"))
        ):
            feed = feed_page(response.content[: self.max_bytes], url, max_chars)
            if feed is not None:
                return feed
            raise FetchError("unsupported_content_type")
        if content_type and content_type not in ALLOWED_CONTENT:
            raise FetchError("unsupported_content_type")
        body = response.content[: self.max_bytes].decode("utf-8", errors="replace")
        text = extract_text(body)
        if not text:
            raise FetchError("empty_page")
        return Page(url=url, text=text[:max_chars], links=tuple(extract_links(body, url)))

    async def fetch_many(self, urls: list[str]) -> list[Page]:
        """Fetch what is reachable, several at a time. A page that fails
        is simply not evidence."""
        reads = asyncio.Semaphore(5)

        async def one(url: str) -> Page | None:
            async with reads:
                try:
                    return await self.fetch(url)
                except FetchError:
                    return None

        read = await asyncio.gather(*(one(url) for url in urls))
        return [page for page in read if page is not None]
