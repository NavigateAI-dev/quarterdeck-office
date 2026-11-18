"""Reading platforms whose public content needs no sign-in.

After Agent Reach: one access path per platform, checked live, with the
ordinary page read as the fallback. Only what can be read without signing in
is read here, in the fetch service, which holds no credentials and does not
imitate a browser: a repository, an issue or a file from GitHub's public API;
a video's details and transcript from YouTube, without downloading the video;
a Bilibili video's details; a V2EX topic and its replies. Feeds are read where
any page is. A platform that cannot answer leaves the page to the ordinary
read, so nothing here can make a page less readable than it was.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from app.tools.fetch import FetchError, Page, assert_public_url

# How long a platform may take before the ordinary read is tried instead.
PLATFORM_TIMEOUT = 45.0
PROBE_TIMEOUT = 20.0
MAX_COMMENTS = 30
MAX_REPLIES = 40
MAX_FEED_ENTRIES = 25
DESCRIPTION_CHARS = 1200
PROBE_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
PROBE_BILIBILI = "BV1xx411c7mD"
JSON = {"Accept": "application/json"}
GITHUB_JSON = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
GITHUB_RAW = {"Accept": "application/vnd.github.raw"}
BILIBILI = {"Accept": "application/json", "Referer": "https://www.bilibili.com/"}

VideoInfo = Callable[[str], dict[str, Any]]


class PlatformError(Exception):
    """This platform could not read it; the ordinary read is tried instead."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _on(host: str, *domains: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def platform_for(url: str) -> str | None:
    """Which platform reader takes this address, if any."""
    host = _host(url)
    path = urlparse(url).path
    if _on(host, "github.com") and host in ("github.com", "www.github.com"):
        return "github" if len([p for p in path.split("/") if p]) >= 2 else None
    if _on(host, "youtube.com", "youtu.be"):
        return "youtube" if _video_id(url) else None
    if _on(host, "bilibili.com") and re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", path):
        return "bilibili"
    if _on(host, "v2ex.com") and re.match(r"^/t/\d+", path):
        return "v2ex"
    return None


def _video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
        candidate = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
    else:
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    return candidate if re.fullmatch(r"[0-9A-Za-z_-]{11}", candidate or "") else None


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    try:
        assert_public_url(url)
    except FetchError:
        raise PlatformError("address_refused") from None
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        raise PlatformError("platform_unreachable") from None
    if response.status_code in (403, 429):
        raise PlatformError("platform_rate_limited")
    if response.status_code != 200:
        raise PlatformError(f"platform_{response.status_code}")
    return response


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise PlatformError("platform_unreadable") from None


def _date(value: Any) -> str:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value, UTC).date().isoformat()
    text = str(value or "")
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def _length(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


# ---- GitHub: the public API, which needs no key for public repositories ----


async def _github(client: httpx.AsyncClient, url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    owner, repo = parts[0], parts[1].removesuffix(".git")
    api = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
    rest = parts[2:]
    if len(rest) >= 2 and rest[0] in ("issues", "pull") and rest[1].isdigit():
        issue = _json(await _get(client, f"{api}/issues/{rest[1]}", GITHUB_JSON))
        comments = _json(
            await _get(
                client, f"{api}/issues/{rest[1]}/comments?per_page={MAX_COMMENTS}", GITHUB_JSON
            )
        )
        kind = "Pull request" if rest[0] == "pull" or issue.get("pull_request") else "Issue"
        lines = [
            f"# {issue.get('title', '')}",
            f"{kind} #{rest[1]} in {owner}/{repo} · {issue.get('state', '')} · opened by "
            f"{(issue.get('user') or {}).get('login', '?')} on {_date(issue.get('created_at'))}",
            "",
            str(issue.get("body") or "(no description)"),
        ]
        for comment in comments if isinstance(comments, list) else []:
            lines += [
                "",
                f"— {(comment.get('user') or {}).get('login', '?')} "
                f"({_date(comment.get('created_at'))}):",
                str(comment.get("body") or ""),
            ]
        return "\n".join(lines)
    if len(rest) >= 3 and rest[0] == "blob":
        ref, path = rest[1], "/".join(rest[2:])
        response = await _get(client, f"{api}/contents/{quote(path)}?ref={quote(ref)}", GITHUB_RAW)
        return f"# {owner}/{repo}: {path} ({ref})\n\n{response.text}"
    if rest and rest[0] not in ("tree",):
        # Releases, discussions, wikis: the ordinary read does them better.
        raise PlatformError("not_a_repository_page")
    details = _json(await _get(client, api, GITHUB_JSON))
    try:
        readme = (await _get(client, f"{api}/readme", GITHUB_RAW)).text
    except PlatformError:
        readme = "(no README)"
    facts = [
        f"{_number(details.get('stargazers_count'))} stars",
        str(details.get("language") or ""),
        ((details.get("license") or {}).get("spdx_id") or "")
        and f"license {(details.get('license') or {}).get('spdx_id')}",
        f"last pushed {_date(details.get('pushed_at'))}",
    ]
    topics = ", ".join(details.get("topics") or [])
    return "\n".join(
        [
            f"# {details.get('full_name') or f'{owner}/{repo}'}",
            str(details.get("description") or ""),
            " · ".join(fact for fact in facts if fact),
            f"Topics: {topics}" if topics else "",
            "",
            "## README",
            readme,
        ]
    )


# ---- YouTube: details and the transcript, never the video itself ----


def youtube_info(url: str) -> dict[str, Any]:
    """What yt-dlp knows about a video, without downloading it."""
    import yt_dlp  # type: ignore[import-untyped]

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(options) as reader:
        info = reader.extract_info(url, download=False)
        return dict(reader.sanitize_info(info) or {})


def _pick_captions(info: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """The track to read: the video's own captions before automatic ones, its
    own language first, a format the office can read."""
    language = str(info.get("language") or "")
    wanted = [f"{language}-orig", language, "en", "en-orig", "zh-Hans", "zh-Hant", "zh", "zh-CN"]
    for source, label in (
        (info.get("subtitles") or {}, "captions"),
        (info.get("automatic_captions") or {}, "automatic captions"),
    ):
        keys = [key for key in wanted if key and key in source] + list(source)
        for key in keys:
            tracks = [t for t in source.get(key) or [] if isinstance(t, dict) and t.get("url")]
            for ext in ("json3", "vtt", "srv3"):
                for track in tracks:
                    if track.get("ext") == ext:
                        return key, label, str(track["url"]), ext
    return None


def _caption_text(body: str, ext: str) -> str:
    if ext == "json3":
        import json

        try:
            events = json.loads(body).get("events") or []
        except (ValueError, AttributeError):
            return ""
        # One caption per event; events are joined with a space, or the end of
        # one line runs into the start of the next ("that?That's").
        said = (
            "".join(str(seg.get("utf8") or "") for seg in event.get("segs") or [])
            for event in events
        )
        return " ".join(" ".join(said).split())
    else:
        kept: list[str] = []
        for line in body.splitlines():
            line = re.sub(r"<[^>]+>", "", line).strip()
            if (
                not line
                or "-->" in line
                or line.isdigit()
                or line.startswith(("WEBVTT", "Kind:", "Language:"))
            ):
                continue
            if not kept or kept[-1] != line:
                kept.append(line)
        text = "\n".join(kept)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def _youtube(client: httpx.AsyncClient, url: str, video_info: VideoInfo) -> str:
    video = _video_id(url)
    canonical = f"https://www.youtube.com/watch?v={video}"
    try:
        info = await asyncio.to_thread(video_info, canonical)
    except Exception:  # noqa: BLE001 - yt-dlp raises many kinds; the page read is the fallback
        raise PlatformError("video_unreadable") from None
    header = [
        f"# {info.get('title') or 'Video'}",
        " · ".join(
            part
            for part in (
                f"Channel: {info.get('channel') or info.get('uploader')}"
                if info.get("channel") or info.get("uploader")
                else "",
                f"published {_date(info.get('upload_date'))}" if info.get("upload_date") else "",
                f"length {_length(info.get('duration'))}" if info.get("duration") else "",
            )
            if part
        ),
        "",
        str(info.get("description") or "")[:DESCRIPTION_CHARS],
    ]
    chosen = _pick_captions(info)
    transcript = ""
    if chosen is not None:
        language, label, caption_url, ext = chosen
        try:
            transcript = _caption_text((await _get(client, caption_url, {})).text, ext)
        except PlatformError:
            transcript = ""
        if transcript:
            header += ["", f"## Transcript ({language}, {label})", transcript]
    if not transcript:
        header += ["", "No transcript is available for this video; only its details are."]
    return "\n".join(header)


# ---- Bilibili and V2EX: their public JSON APIs ----


async def _bilibili(client: httpx.AsyncClient, url: str) -> str:
    match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av(\d+))", urlparse(url).path)
    if match is None:
        raise PlatformError("not_a_video")
    query = f"aid={match.group(2)}" if match.group(2) else f"bvid={match.group(1)}"
    body = _json(
        await _get(client, f"https://api.bilibili.com/x/web-interface/view?{query}", BILIBILI)
    )
    if body.get("code") != 0 or not isinstance(body.get("data"), dict):
        raise PlatformError("platform_refused")
    data = body["data"]
    stat = data.get("stat") or {}
    lines = [
        f"# {data.get('title') or 'Video'}",
        " · ".join(
            part
            for part in (
                f"UP: {(data.get('owner') or {}).get('name')}"
                if (data.get("owner") or {}).get("name")
                else "",
                f"published {_date(data.get('pubdate'))}" if data.get("pubdate") else "",
                f"length {_length(data.get('duration'))}" if data.get("duration") else "",
                f"{_number(stat.get('view'))} views" if stat.get("view") is not None else "",
                f"{_number(stat.get('like'))} likes" if stat.get("like") is not None else "",
            )
            if part
        ),
        "",
        str(data.get("desc") or "")[:DESCRIPTION_CHARS],
    ]
    tracks = ((data.get("subtitle") or {}).get("list")) or []
    subtitle_url = next((str(t.get("subtitle_url")) for t in tracks if t.get("subtitle_url")), "")
    if subtitle_url:
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        try:
            said = _json(await _get(client, subtitle_url, BILIBILI))
            text = "\n".join(str(row.get("content") or "") for row in said.get("body") or [])
            if text:
                lines += ["", "## Subtitles", text]
        except PlatformError:
            pass
    else:
        lines += ["", "No subtitles are offered for this video without signing in."]
    return "\n".join(lines)


async def _v2ex(client: httpx.AsyncClient, url: str) -> str:
    topic = re.match(r"^/t/(\d+)", urlparse(url).path)
    if topic is None:
        raise PlatformError("not_a_topic")
    shown = _json(
        await _get(client, f"https://www.v2ex.com/api/topics/show.json?id={topic.group(1)}", JSON)
    )
    if not isinstance(shown, list) or not shown:
        raise PlatformError("not_found")
    first = shown[0]
    replies = _json(
        await _get(
            client, f"https://www.v2ex.com/api/replies/show.json?topic_id={topic.group(1)}", JSON
        )
    )
    lines = [
        f"# {first.get('title') or 'Topic'}",
        f"V2EX · {(first.get('node') or {}).get('title', '')} · by "
        f"{(first.get('member') or {}).get('username', '?')} on {_date(first.get('created'))}",
        "",
        str(first.get("content") or ""),
    ]
    for reply in (replies if isinstance(replies, list) else [])[:MAX_REPLIES]:
        lines += [
            "",
            f"— {(reply.get('member') or {}).get('username', '?')} "
            f"({_date(reply.get('created'))}):",
            str(reply.get("content") or ""),
        ]
    return "\n".join(lines)


async def read_platform(
    url: str, client: httpx.AsyncClient, max_chars: int, video_info: VideoInfo
) -> Page:
    """Read one address through its platform, or raise PlatformError."""
    kind = platform_for(url)
    readers: dict[str, Callable[[], Awaitable[str]]] = {
        "github": lambda: _github(client, url),
        "youtube": lambda: _youtube(client, url, video_info),
        "bilibili": lambda: _bilibili(client, url),
        "v2ex": lambda: _v2ex(client, url),
    }
    if kind is None:
        raise PlatformError("no_platform")
    try:
        async with asyncio.timeout(PLATFORM_TIMEOUT):
            text = await readers[kind]()
    except TimeoutError:
        raise PlatformError("platform_timeout") from None
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not text:
        raise PlatformError("empty")
    return Page(url=url, text=text[:max_chars], read_via=kind)


# ---- Feeds: read where any page is ----

FEED_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
        "application/feed+json",
    }
)


def feed_page(body: bytes, url: str, max_chars: int) -> Page | None:
    """A feed's entries, if the body is a feed at all."""
    import feedparser  # type: ignore[import-untyped]

    parsed = feedparser.parse(body)
    entries = list(parsed.get("entries") or [])
    if not entries:
        return None
    feed = parsed.get("feed") or {}
    lines = [f"# {feed.get('title') or 'Feed'}", str(feed.get("subtitle") or ""), ""]
    for entry in entries[:MAX_FEED_ENTRIES]:
        when = entry.get("published") or entry.get("updated") or ""
        summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary") or ""))
        lines += [
            f"## {entry.get('title') or '(untitled)'}",
            " · ".join(part for part in (str(when), str(entry.get("link") or "")) if part),
            " ".join(summary.split())[:600],
            "",
        ]
    links = tuple(
        (str(entry.get("title") or ""), str(entry.get("link")))
        for entry in entries[:MAX_FEED_ENTRIES]
        if str(entry.get("link") or "").startswith("https://")
    )
    return Page(url=url, text="\n".join(lines).strip()[:max_chars], links=links, read_via="feed")


# ---- What the office can reach ----

SOURCES = (
    ("web", "Web pages", "Any public page, read directly", None),
    (
        "reader",
        "Reader fallback",
        "Pages that refuse the office, read through Jina Reader",
        "r.jina.ai",
    ),
    ("github", "GitHub", "Repositories, READMEs, issues, pull requests and files", "github.com"),
    ("youtube", "YouTube", "A video's details and transcript", "youtube.com"),
    ("bilibili", "Bilibili", "A video's details, and subtitles when offered", "bilibili.com"),
    ("v2ex", "V2EX", "A topic and its replies", "v2ex.com"),
    ("feeds", "RSS and Atom feeds", "A feed's latest entries", None),
)


async def probe_all(
    client: httpx.AsyncClient,
    allow_list: list[str],
    reader_base: str | None,
    video_info: VideoInfo,
) -> list[dict[str, Any]]:
    """Each source, checked from where the office reads, with how to fix it."""
    from app.tools.fetch_service import host_permitted

    async def web() -> tuple[str, str, str, str]:
        response = await client.get("https://www.example.com")
        if response.status_code == 200:
            return "ok", "direct", "Pages are read directly.", ""
        return (
            "warn",
            "direct",
            f"A test page answered {response.status_code}.",
            "Check this computer's internet connection.",
        )

    async def reader() -> tuple[str, str, str, str]:
        if not reader_base:
            return (
                "off",
                "",
                "Switched off: a page that refuses the office stays unread.",
                "Set READER_BASE_URL for the fetcher (https://r.jina.ai/) to turn it on.",
            )
        response = await client.get(f"{reader_base}https://www.example.com")
        if response.status_code == 200:
            return "ok", "Jina Reader", "Answers; used only after a page refuses the office.", ""
        return (
            "warn",
            "Jina Reader",
            f"Answered {response.status_code}.",
            "It may be busy; it is tried again on the next page.",
        )

    async def github() -> tuple[str, str, str, str]:
        body = _json(await _get(client, "https://api.github.com/rate_limit", GITHUB_JSON))
        core = (body.get("resources") or {}).get("core") or {}
        left, limit = core.get("remaining"), core.get("limit")
        if left:
            return "ok", "public API", f"{left} of {limit} requests left this hour.", ""
        return (
            "warn",
            "public API",
            "No requests left this hour.",
            "It resets within the hour; GitHub pages are read as pages meanwhile.",
        )

    async def youtube() -> tuple[str, str, str, str]:
        try:
            import yt_dlp

            version = yt_dlp.version.__version__
        except ImportError:
            return (
                "off",
                "",
                "yt-dlp is not installed in the fetcher.",
                "Rebuild the office with python3 -m scripts.upgrade.",
            )
        try:
            await asyncio.to_thread(video_info, PROBE_VIDEO)
        except Exception as failed:  # noqa: BLE001 - what YouTube said is the detail
            said = " ".join(str(failed).split())[:200]
            return (
                "warn",
                f"yt-dlp {version}",
                said or "YouTube did not answer.",
                "YouTube sometimes asks a network to sign in; videos are then read as "
                "pages. Upgrading the office updates yt-dlp.",
            )
        return (
            "ok",
            f"yt-dlp {version}",
            "Reads details and transcripts; never downloads a video.",
            "",
        )

    async def bilibili() -> tuple[str, str, str, str]:
        body = _json(
            await _get(
                client,
                f"https://api.bilibili.com/x/web-interface/view?bvid={PROBE_BILIBILI}",
                BILIBILI,
            )
        )
        if body.get("code") == 0:
            return (
                "ok",
                "public API",
                "Reads details; subtitles only when offered without signing in.",
                "",
            )
        return (
            "warn",
            "public API",
            f"Answered code {body.get('code')}.",
            "Bilibili may limit this network; its pages are read as pages meanwhile.",
        )

    async def v2ex() -> tuple[str, str, str, str]:
        await _get(client, "https://www.v2ex.com/api/site/info.json", JSON)
        return "ok", "public API", "Reads topics and replies.", ""

    async def feeds() -> tuple[str, str, str, str]:
        try:
            import feedparser  # noqa: F401
        except ImportError:
            return (
                "off",
                "",
                "feedparser is not installed in the fetcher.",
                "Rebuild the office with python3 -m scripts.upgrade.",
            )
        return "ok", "feedparser", "Any RSS or Atom feed.", ""

    checks: dict[str, Callable[[], Awaitable[tuple[str, str, str, str]]]] = {
        "web": web,
        "reader": reader,
        "github": github,
        "youtube": youtube,
        "bilibili": bilibili,
        "v2ex": v2ex,
        "feeds": feeds,
    }

    async def one(source: str, name: str, what: str, host: str | None) -> dict[str, Any]:
        row = {"id": source, "name": name, "what": what, "host": host}
        if host and allow_list and not host_permitted(host, allow_list):
            return row | {
                "state": "off",
                "via": "",
                "detail": "Not on the office's allow-list.",
                "fix": f"Add {host} to FETCH_ALLOW_HOSTS for the fetcher, or clear the list.",
            }
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                state, via, detail, fix = await checks[source]()
        except (PlatformError, httpx.HTTPError, TimeoutError) as failed:
            code = getattr(failed, "code", None) or type(failed).__name__
            state, via, detail, fix = (
                "warn",
                "",
                f"Did not answer ({code}).",
                "Check this computer's internet connection; it is checked again next time.",
            )
        return row | {"state": state, "via": via, "detail": detail, "fix": fix}

    return list(await asyncio.gather(*(one(*source) for source in SOURCES)))
