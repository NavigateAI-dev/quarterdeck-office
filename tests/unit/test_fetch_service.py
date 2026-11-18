"""Reading pages away from the office's credentials."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.tools.fetch import FetchError, Page
from app.tools.fetch_client import ExchangeFetcher, fetcher_heartbeat_age
from app.tools.fetch_service import EXCHANGE_JOBS, handle, host_permitted, serve_once


def test_with_no_list_every_public_host_is_permitted() -> None:
    assert host_permitted("example.com", []) is True


def test_a_list_permits_its_hosts_and_their_subdomains_only() -> None:
    allow = ["example.com", "gov.uk"]
    assert host_permitted("example.com", allow) is True
    assert host_permitted("docs.example.com", allow) is True
    assert host_permitted("tax.service.gov.uk", allow) is True
    assert host_permitted("example.com.attacker.test", allow) is False
    assert host_permitted("elsewhere.test", allow) is False


class Recording:
    """Stands in for the page reader."""

    def __init__(self, page: Page | None = None, error: str | None = None) -> None:
        self.page = page
        self.error = error
        self.asked: list[str] = []

    async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 1000) -> Page:
        self.asked.append(url)
        if self.error:
            raise FetchError(self.error)
        assert self.page is not None
        return self.page


async def test_a_host_outside_the_list_is_never_requested() -> None:
    reader = Recording(Page(url="https://example.com", text="hello"))
    answer = await handle({"url": "https://elsewhere.test/x"}, reader, ["example.com"])
    assert answer == {"error": "host_not_allowed"}
    assert reader.asked == [], "the service opened a connection it should have refused"


async def test_a_page_comes_back_with_its_text_links_and_time() -> None:
    page = Page(
        url="https://example.com/a", text="Prices", links=(("Next", "https://example.com/b"),)
    )
    answer = await handle({"url": "https://example.com/a"}, Recording(page), [])
    assert answer["page"]["text"] == "Prices"
    assert answer["page"]["links"] == [["Next", "https://example.com/b"]]
    assert answer["page"]["retrieved_at"]


async def test_a_refusal_comes_back_as_its_reason_not_a_crash() -> None:
    answer = await handle({"url": "https://example.com"}, Recording(error="non_public_address"), [])
    assert answer == {"error": "non_public_address"}


async def test_the_office_asks_through_the_exchange_and_reads_the_answer(tmp_path: Path) -> None:
    page = Page(url="https://example.com/a", text="Prices", links=())
    reader = Recording(page)

    async def service() -> None:
        for _ in range(100):
            if await serve_once(tmp_path, reader):  # type: ignore[arg-type]
                return
            await asyncio.sleep(0.02)

    fetcher = ExchangeFetcher(tmp_path, timeout_seconds=5)
    running = asyncio.create_task(service())
    got = await fetcher.fetch("https://example.com/a")
    await running
    assert got.text == "Prices" and got.url == "https://example.com/a"
    assert reader.asked == ["https://example.com/a"]


async def test_a_page_that_never_answers_is_not_evidence(tmp_path: Path) -> None:
    fetcher = ExchangeFetcher(tmp_path, timeout_seconds=0.3, poll=0.05)
    with pytest.raises(FetchError) as refusal:
        await fetcher.fetch("https://example.com/a")
    assert refusal.value.code == "fetcher_unavailable"
    # The job is withdrawn, so a service starting later does not fetch it.
    assert list((tmp_path / EXCHANGE_JOBS).glob("*.job.json")) == []
    assert await fetcher.fetch_many(["https://example.com/a"]) == []


def test_a_job_that_is_not_ours_is_dropped_rather_than_run(tmp_path: Path) -> None:
    jobs = tmp_path / EXCHANGE_JOBS
    jobs.mkdir(parents=True)
    (jobs / "bad.job.json").write_text(json.dumps({"id": "../escape", "url": "https://x.test"}))
    reader = Recording(Page(url="https://x.test", text="x"))
    assert asyncio.run(serve_once(tmp_path, reader)) is True  # type: ignore[arg-type]
    assert reader.asked == []
    assert list(jobs.glob("*.result.json")) == []


def test_the_page_reader_holds_no_credentials() -> None:
    """The service must not be able to reach the office's secrets at all."""
    import app.tools.fetch_service as service

    imports = [
        line
        for line in Path(service.__file__).read_text().splitlines()
        if line.startswith(("import ", "from "))
    ]
    for forbidden in ("secret_store", "credentials", "db.session", "db.models", "sqlalchemy"):
        offending = [line for line in imports if forbidden in line]
        assert offending == [], f"the page reader imports {forbidden}: {offending}"
    # Nor may anything it imports pull them in transitively.
    from app.tools import fetch

    fetch_imports = [
        line
        for line in Path(fetch.__file__).read_text().splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert not [line for line in fetch_imports if "app." in line and "tools" not in line]


def test_the_office_can_tell_whether_the_reader_is_running(tmp_path: Path) -> None:
    assert fetcher_heartbeat_age(tmp_path) is None
    (tmp_path / "fetcher.heartbeat").write_text("0")
    age = fetcher_heartbeat_age(tmp_path)
    assert age is not None and age > 0


def test_the_service_reads_its_allow_list_from_the_environment(monkeypatch: Any) -> None:
    from app.tools.fetch_service import allowed_hosts

    monkeypatch.setenv("FETCH_ALLOW_HOSTS", " docs.example.com , *.gov.uk ")
    assert allowed_hosts() == ["docs.example.com", "gov.uk"]
    monkeypatch.setenv("FETCH_ALLOW_HOSTS", "")
    assert allowed_hosts() == []
