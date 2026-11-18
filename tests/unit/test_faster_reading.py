"""Faster, surer page reading, on fake services: nothing is fetched."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.tools import fetch_client
from app.tools.fetch import FetchError
from app.tools.fetch_client import BROWSER_READS, CONCURRENT_READS, ExchangeFetcher
from app.tools.fetch_service import EXCHANGE_JOBS, claim


def page(url: str, text: str = "A page with plenty of words. " * 20) -> dict[str, Any]:
    return {
        "page": {"url": url, "text": text, "retrieved_at": None, "links": [], "read_via": "direct"}
    }


async def test_pages_are_read_several_at_a_time_in_the_order_asked(tmp_path: Path) -> None:
    reader = ExchangeFetcher(tmp_path)
    now = {"open": 0, "most": 0}

    async def request(body: dict[str, Any]) -> dict[str, Any]:
        now["open"] += 1
        now["most"] = max(now["most"], now["open"])
        await asyncio.sleep(0.02)
        now["open"] -= 1
        if "bad" in body["url"]:
            return {"error": "host_not_allowed"}
        return page(body["url"])

    reader._request = request  # type: ignore[method-assign]
    urls = [f"https://site{n}.example/" for n in range(9)] + ["https://bad.example/"]
    pages = await reader.fetch_many(urls)
    assert [p.url for p in pages] == urls[:9]
    assert 1 < now["most"] <= CONCURRENT_READS


async def test_a_page_read_once_is_kept_for_the_task(tmp_path: Path) -> None:
    calls: list[str] = []
    reader = ExchangeFetcher(tmp_path)

    async def request(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body["url"])
        return page(body["url"])

    reader._request = request  # type: ignore[method-assign]
    await reader.fetch("https://a.example/")
    await reader.fetch("https://a.example/")
    assert calls == ["https://a.example/"]


class Browser:
    """The office's own browser, faked: it shows what a script-only page renders."""

    opened: list[str] = []

    def __init__(self, exchange: Path) -> None:
        return None

    def running(self) -> bool:
        return True

    async def open(self) -> dict[str, Any]:
        return {}

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        Browser.opened.append(action["text"])
        return {"page": {"url": action["text"], "text": "Rendered prices: Pro $59 a month. " * 5}}

    async def close(self) -> None:
        return None


async def test_a_page_that_refuses_is_opened_in_the_offices_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.tools.fetch_client.ExchangeBrowser", Browser)
    Browser.opened = []
    seen: list[dict[str, Any]] = []
    reader = ExchangeFetcher(tmp_path, browser_exchange=tmp_path, on_read=seen.append)

    async def request(body: dict[str, Any]) -> dict[str, Any]:
        if "shell" in body["url"]:
            return page(body["url"], text="Loading…")
        if "allowed" in body["url"]:
            return {"error": "host_not_allowed"}
        return {"error": "fetch_rejected_403"}

    reader._request = request  # type: ignore[method-assign]
    refused = await reader.fetch("https://refuses.example/pricing")
    assert refused.read_via == "browser" and "Pro $59" in refused.text
    shell = await reader.fetch("https://shell.example/")
    assert shell.read_via == "browser", "a script-only shell is rendered"
    with pytest.raises(FetchError, match="host_not_allowed"):
        await reader.fetch("https://notallowed.example/")
    assert Browser.opened == ["https://refuses.example/pricing", "https://shell.example/"]
    assert [(r["via"], r["ok"]) for r in seen] == [
        ("browser", True),
        ("browser", True),
        ("failed", False),
    ]
    assert seen[2]["code"] == "host_not_allowed" and seen[0]["host"] == "refuses.example"


async def test_the_browser_is_used_sparingly_and_only_when_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.tools.fetch_client.ExchangeBrowser", Browser)
    Browser.opened = []

    async def refuse(body: dict[str, Any]) -> dict[str, Any]:
        return {"error": "fetch_rejected_403"}

    without = ExchangeFetcher(tmp_path)  # browsing is off: no browser given
    without._request = refuse  # type: ignore[method-assign]
    with pytest.raises(FetchError):
        await without.fetch("https://a.example/")
    assert Browser.opened == []

    capped = ExchangeFetcher(tmp_path, browser_exchange=tmp_path)
    capped._request = refuse  # type: ignore[method-assign]
    for n in range(BROWSER_READS + 2):
        try:
            await capped.fetch(f"https://p{n}.example/")
        except FetchError:
            pass
    assert len(Browser.opened) == BROWSER_READS


def test_the_page_service_takes_each_job_once(tmp_path: Path) -> None:
    jobs = tmp_path / EXCHANGE_JOBS
    jobs.mkdir()
    for n in range(3):
        (jobs / f"j{n}.job.json").write_text(json.dumps({"id": f"j{n}", "url": "https://a/"}))
    taken = [claim(tmp_path) for _ in range(4)]
    assert taken[3] is None
    assert sorted(path.name for path in taken[:3] if path) == [
        "j0.job.running",
        "j1.job.running",
        "j2.job.running",
    ]


def test_reading_is_measured_without_failing(tmp_path: Path) -> None:
    def broken(read: dict[str, Any]) -> None:
        raise RuntimeError("the recorder is down")

    reader = ExchangeFetcher(tmp_path, on_read=broken)
    reader._report("https://a.example/", 0.0, None, "fetch_failed")
    assert fetch_client.THIN_PAGE > 0
