"""A room a routine feeds opens on its newest run, with an index of its runs."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from driver import Page, browser, find_browser
from throwaway import database_ready, running_office, sign_in_url

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def office() -> Iterator[dict[str, Any]]:
    if not database_ready():
        pytest.skip("Set TEST_DATABASE_URL to run on a throwaway office")
    if find_browser() is None:
        pytest.skip("no browser installed")
    with running_office() as started:
        yield started


@pytest.fixture
async def page(office: dict[str, Any]) -> AsyncIterator[Page]:
    async with browser(sign_in_url(office)) as open_page:
        await open_page.settle(2.0)
        await open_page.evaluate("localStorage.removeItem('office.roomDays')")
        yield open_page


async def open_recaps(page: Page, office: dict[str, Any], width: int = 1600) -> None:
    await page.viewport(width, 900)
    await page.evaluate(f"location.hash = 'topic/{office['recap_topic']}'")
    await page.settle(3.0)


async def read(page: Page, script: str) -> Any:
    return json.loads(await page.evaluate(f"JSON.stringify(({script})())"))


async def test_the_index_lists_runs_newest_first_and_folds_older_months(
    page: Page, office: dict[str, Any]
) -> None:
    await open_recaps(page, office)
    index = await read(
        page,
        """() => ({
          shown: !document.querySelector('#room-days').hidden,
          first: document.querySelector('#room-days .room-days-go').textContent,
          recent: document.querySelectorAll('.room-days-list > li > .room-days-go').length,
          months: [...document.querySelectorAll('.room-days-month summary')]
            .map((s) => s.textContent.trim()),
          beside: document.querySelector('#room-messages').classList.contains('days-beside'),
        })""",
    )
    assert index["shown"] and index["beside"], index
    assert index["first"] == "05 Nov 2026", index
    assert index["recent"] == 7, index
    assert index["months"] == ["October 2026 3"], index
    assert page.console == []


async def test_the_room_opens_on_the_newest_run(page: Page, office: dict[str, Any]) -> None:
    await open_recaps(page, office)
    top = await read(
        page,
        """() => {
          const runs = [...document.querySelectorAll('#room-feed .room-run')];
          return {
            newest: runs.at(-1).getBoundingClientRect().top,
            oldest: runs[0].getBoundingClientRect().top,
          };
        }""",
    )
    assert 0 <= top["newest"] < 300, top
    assert top["oldest"] < 0, top


async def test_a_date_jumps_to_its_run(page: Page, office: dict[str, Any]) -> None:
    await open_recaps(page, office)
    await page.evaluate(
        "[...document.querySelectorAll('.room-days-go')]"
        ".find((b) => b.textContent === '02 Nov 2026').click()"
    )
    await page.settle(1.5)
    jumped = await read(
        page,
        """() => {
          const button = [...document.querySelectorAll('.room-days-go')]
            .find((b) => b.textContent === '02 Nov 2026');
          const run = document.querySelector(`#room-feed [data-mid="${button.dataset.run}"]`);
          return {
            top: run.getBoundingClientRect().top,
            current: button.getAttribute('aria-current'),
          };
        }""",
    )
    assert 0 <= jumped["top"] < 300, jumped
    assert jumped["current"] == "true", jumped


async def test_the_instructions_show_once_folded(page: Page, office: dict[str, Any]) -> None:
    await open_recaps(page, office)
    asks = await read(
        page,
        """() => ({
          runs: document.querySelectorAll('#room-feed .room-run').length,
          inFeed: [...document.querySelectorAll('#room-feed .room-run')]
            .filter((li) => li.innerText.includes('Section 12')).length,
          folds: document.querySelectorAll('#room-days .room-days-asks').length,
          open: document.querySelector('#room-days .room-days-asks').open,
          title: document.querySelector('#room-feed .room-run strong').textContent,
        })""",
    )
    assert asks == {"runs": 10, "inFeed": 0, "folds": 1, "open": False, "title": "Morning recap"}


async def test_the_index_folds_and_stays_folded(page: Page, office: dict[str, Any]) -> None:
    await open_recaps(page, office)
    await page.evaluate("document.querySelector('.room-days-toggle').click()")
    await open_recaps(page, office)
    folded = await read(
        page,
        """() => ({
          body: document.querySelector('#room-days-body').hidden,
          expanded: document.querySelector('.room-days-toggle').getAttribute('aria-expanded'),
        })""",
    )
    assert folded == {"body": True, "expanded": "false"}


async def test_on_a_phone_the_index_sits_above_the_thread_folded(
    page: Page, office: dict[str, Any]
) -> None:
    await open_recaps(page, office, width=390)
    phone = await read(
        page,
        """() => ({
          beside: document.querySelector('#room-messages').classList.contains('days-beside'),
          body: document.querySelector('#room-days-body').hidden,
          wider: document.documentElement.scrollWidth > window.innerWidth,
        })""",
    )
    assert phone == {"beside": False, "body": True, "wider": False}


async def test_on_a_phone_a_date_folds_the_index_and_shows_its_run(
    page: Page, office: dict[str, Any]
) -> None:
    await open_recaps(page, office, width=390)
    await page.evaluate("document.querySelector('.room-days-toggle').click()")
    await page.evaluate(
        "[...document.querySelectorAll('.room-days-go')]"
        ".find((b) => b.textContent === '02 Nov 2026').click()"
    )
    await page.settle(1.5)
    shown = await read(
        page,
        """() => {
          const button = [...document.querySelectorAll('.room-days-go')]
            .find((b) => b.textContent === '02 Nov 2026');
          const run = document.querySelector(`#room-feed [data-mid="${button.dataset.run}"]`);
          return {
            folded: document.querySelector('#room-days-body').hidden,
            below: run.getBoundingClientRect().top
              >= document.querySelector('#room-days').getBoundingClientRect().bottom,
            inView: run.getBoundingClientRect().top < window.innerHeight / 2,
          };
        }""",
    )
    assert shown == {"folded": True, "below": True, "inView": True}
