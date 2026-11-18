"""The lead's proposal of a full team run, started from its task in a real browser."""

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
        yield open_page


async def test_the_proposal_is_started_from_its_task(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate(f"location.hash = 'task/{office['proposing']}'")
    await page.settle(3.0)
    card = await page.evaluate(
        "document.querySelector('[data-action=\"team-run\"]')"
        "?.closest('.attention')?.innerText || ''"
    )
    assert "The lead proposes a full team run" in card
    assert "Build the Slack self-check tool" in card
    assert "Start building there" in card, "says where Start building comes, later"

    await page.evaluate("document.querySelector('[data-action=\"team-run\"]').click()")
    await page.settle(0.8)
    await page.evaluate("document.querySelector('[data-action=\"team-run-go\"]').click()")
    await page.settle(2.5)

    opened = await page.evaluate("location.hash")
    assert opened.startswith("#task/") and office["proposing"] not in opened, "the team's task"
    await page.evaluate(f"location.hash = 'task/{office['proposing']}'")
    await page.settle(3.0)
    done = await page.evaluate("document.querySelector('.attention')?.innerText || ''")
    assert "The full team is on it" in done
    assert await page.evaluate("!document.querySelector('[data-action=\"team-run\"]')")
