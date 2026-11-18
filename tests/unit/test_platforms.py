"""RE1: reading platforms whose public content needs no sign-in, in the fetch service."""

from typing import Any

import httpx
import pytest

from app.tools.fetch import PageFetcher
from app.tools.platforms import platform_for, probe_all


@pytest.fixture(autouse=True)
def resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin DNS: these exercise the readers, not the network."""
    import socket

    def resolved(host: str, port: int, *args: object, **kwargs: object) -> list:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolved)


def site(routes: dict[str, Any]) -> httpx.MockTransport:
    """Answer by URL prefix; a callable answers with the request."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        for prefix, answer in routes.items():
            if url.startswith(prefix):
                if callable(answer):
                    return answer(request)
                if isinstance(answer, httpx.Response):
                    return answer
                return httpx.Response(200, json=answer)
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def fetcher(transport: httpx.MockTransport, **kw: Any) -> PageFetcher:
    return PageFetcher(transport=transport, host_interval=0, retry_base=0, reader_base=None, **kw)


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://github.com/acme/tool", "github"),
        ("https://www.youtube.com/watch?v=abc123DEF45", "youtube"),
        ("https://youtu.be/abc123DEF45", "youtube"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
        ("https://www.v2ex.com/t/1234567", "v2ex"),
        ("https://example.com/report", None),
        ("https://github.com.evil.example/acme/tool", None),
    ],
)
def test_each_platform_is_recognised_by_its_own_host(url: str, kind: str | None) -> None:
    assert platform_for(url) == kind


async def test_a_repository_is_read_with_its_readme() -> None:
    transport = site(
        {
            "https://api.github.com/repos/acme/tool/readme": httpx.Response(
                200, text="# Tool\nIt does one thing well."
            ),
            "https://api.github.com/repos/acme/tool": {
                "full_name": "acme/tool",
                "description": "A small tool",
                "stargazers_count": 1200,
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "topics": ["agents"],
                "pushed_at": "2026-09-15T16:16:24Z",
            },
        }
    )
    async with fetcher(transport) as reader:
        page = await reader.fetch("https://github.com/acme/tool")
    assert page.read_via == "github"
    assert "acme/tool" in page.text and "A small tool" in page.text
    assert "1,200 stars" in page.text and "MIT" in page.text
    assert "It does one thing well." in page.text


async def test_an_issue_is_read_with_its_discussion() -> None:
    transport = site(
        {
            "https://api.github.com/repos/acme/tool/issues/7/comments": [
                {
                    "user": {"login": "bea"},
                    "created_at": "2026-09-02T10:00:00Z",
                    "body": "Same here.",
                }
            ],
            "https://api.github.com/repos/acme/tool/issues/7": {
                "title": "Crash on start",
                "state": "open",
                "user": {"login": "ann"},
                "created_at": "2026-09-01T10:00:00Z",
                "body": "It crashes.",
            },
        }
    )
    async with fetcher(transport) as reader:
        page = await reader.fetch("https://github.com/acme/tool/issues/7")
    assert "Crash on start" in page.text and "It crashes." in page.text
    assert "bea" in page.text and "Same here." in page.text


async def test_a_platform_that_cannot_answer_falls_back_to_the_ordinary_read() -> None:
    transport = site(
        {
            "https://api.github.com/": httpx.Response(403, json={"message": "rate limit"}),
            "https://github.com/acme/tool": httpx.Response(
                200,
                text="<html><body><h1>acme/tool</h1><p>Page text</p></body></html>",
                headers={"content-type": "text/html"},
            ),
        }
    )
    async with fetcher(transport) as reader:
        page = await reader.fetch("https://github.com/acme/tool")
    assert page.read_via == "direct" and "Page text" in page.text


async def test_a_video_is_read_as_its_transcript_without_downloading_it() -> None:
    asked: list[str] = []

    def video_info(url: str) -> dict[str, Any]:
        asked.append(url)
        return {
            "title": "Agents in production",
            "channel": "Builders",
            "upload_date": "20260901",
            "duration": 754,
            "language": "en",
            "description": "A talk about running agents.",
            "subtitles": {},
            "automatic_captions": {
                "fr": [{"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=fr"}],
                "en": [
                    {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt"},
                    {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=en"},
                ],
            },
        }

    events = {
        "events": [
            {"segs": [{"utf8": "Hello "}, {"utf8": "builders"}]},
            {"segs": [{"utf8": "\n"}]},
            {"segs": [{"utf8": "Agents need limits."}]},
        ]
    }
    transport = site(
        {"https://www.youtube.com/api/timedtext?lang=en": httpx.Response(200, json=events)}
    )
    async with fetcher(transport, video_info=video_info) as reader:
        page = await reader.fetch("https://youtu.be/abc123DEF45")
    assert page.read_via == "youtube"
    assert asked == ["https://www.youtube.com/watch?v=abc123DEF45"]
    assert "Agents in production" in page.text and "Builders" in page.text
    assert "12:34" in page.text, "the length is given"
    assert "Hello builders" in page.text and "Agents need limits." in page.text
    assert "automatic captions" in page.text.lower()


async def test_a_video_without_captions_says_so() -> None:
    def video_info(url: str) -> dict[str, Any]:
        return {
            "title": "Silent",
            "channel": "C",
            "description": "d",
            "subtitles": {},
            "automatic_captions": {},
        }

    async with fetcher(site({}), video_info=video_info) as reader:
        page = await reader.fetch("https://www.youtube.com/watch?v=abc123DEF45")
    assert "no transcript" in page.text.lower()


async def test_a_bilibili_video_is_read_from_its_public_details() -> None:
    transport = site(
        {
            "https://api.bilibili.com/x/web-interface/view": {
                "code": 0,
                "data": {
                    "title": "短剧的经济账",
                    "desc": "成本与回报",
                    "owner": {"name": "UP主"},
                    "pubdate": 1756700000,
                    "duration": 300,
                    "stat": {"view": 12000, "like": 800},
                    "subtitle": {"list": []},
                },
            }
        }
    )
    async with fetcher(transport) as reader:
        page = await reader.fetch("https://www.bilibili.com/video/BV1xx411c7mD")
    assert page.read_via == "bilibili"
    assert "短剧的经济账" in page.text and "UP主" in page.text and "成本与回报" in page.text


async def test_a_v2ex_topic_is_read_with_its_replies() -> None:
    transport = site(
        {
            "https://www.v2ex.com/api/topics/show.json": [
                {
                    "title": "Which agent framework?",
                    "content": "Choosing one.",
                    "member": {"username": "lee"},
                    "node": {"title": "AI"},
                    "created": 1756700000,
                }
            ],
            "https://www.v2ex.com/api/replies/show.json": [
                {"content": "Keep it simple.", "member": {"username": "kim"}, "created": 1756703600}
            ],
        }
    )
    async with fetcher(transport) as reader:
        page = await reader.fetch("https://www.v2ex.com/t/1234567")
    assert page.read_via == "v2ex"
    assert (
        "Which agent framework?" in page.text
        and "Keep it simple." in page.text
        and "kim" in page.text
    )


async def test_a_feed_is_read_as_its_entries() -> None:
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>Builders blog</title>
      <item><title>Agents in production</title><link>https://blog.example.com/a</link>
      <pubDate>Mon, 29 Sep 2026 10:00:00 GMT</pubDate>
      <description>What we learned.</description></item>
    </channel></rss>"""
    transport = site(
        {
            "https://blog.example.com/feed.xml": httpx.Response(
                200, text=feed, headers={"content-type": "application/rss+xml"}
            )
        }
    )
    async with fetcher(transport) as reader:
        page = await reader.fetch("https://blog.example.com/feed.xml")
    assert page.read_via == "feed"
    assert "Builders blog" in page.text and "Agents in production" in page.text
    assert "https://blog.example.com/a" in page.text and "What we learned." in page.text


async def test_a_platform_reader_keeps_to_public_addresses() -> None:
    def video_info(url: str) -> dict[str, Any]:
        return {
            "title": "t",
            "subtitles": {"en": [{"ext": "json3", "url": "https://127.0.0.1/captions"}]},
        }

    async with fetcher(site({}), video_info=video_info) as reader:
        page = await reader.fetch("https://www.youtube.com/watch?v=abc123DEF45")
    assert "no transcript" in page.text.lower(), "a private caption address is not read"


# What the office can reach.


async def test_the_probe_says_what_works_and_how_to_fix_what_does_not() -> None:
    transport = site(
        {
            "https://www.example.com": httpx.Response(200, text="<p>ok</p>"),
            "https://api.github.com/rate_limit": {
                "resources": {"core": {"remaining": 42, "limit": 60}}
            },
            "https://api.bilibili.com/": {"code": 0, "data": {"title": "x"}},
            "https://www.v2ex.com/api/site/info.json": {"title": "V2EX"},
        }
    )

    def video_info(url: str) -> dict[str, Any]:
        raise RuntimeError("Sign in to confirm you're not a bot")

    async with httpx.AsyncClient(transport=transport) as client:
        found = {
            row["id"]: row
            for row in await probe_all(client, [], reader_base=None, video_info=video_info)
        }
    assert found["web"]["state"] == "ok"
    assert found["reader"]["state"] == "off" and "READER_BASE_URL" in found["reader"]["fix"]
    assert found["github"]["state"] == "ok" and "42" in found["github"]["detail"]
    assert found["youtube"]["state"] == "warn" and "bot" in found["youtube"]["detail"]
    assert found["bilibili"]["state"] == "ok" and found["v2ex"]["state"] == "ok"
    assert found["feeds"]["state"] == "ok"


async def test_a_source_off_the_allow_list_says_so() -> None:
    async with httpx.AsyncClient(transport=site({})) as client:
        found = {
            row["id"]: row
            for row in await probe_all(
                client, ["example.com"], reader_base=None, video_info=lambda url: {}
            )
        }
    assert found["github"]["state"] == "off"
    assert "FETCH_ALLOW_HOSTS" in found["github"]["fix"] and "github.com" in found["github"]["fix"]
