"""First-run onboarding, in place of Setup (13e): on a new office only."""

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
        await open_page.settle(2.5)
        yield open_page


async def test_an_office_with_work_is_not_walked_through_setup(page: Page) -> None:
    assert not await page.evaluate("!!document.getElementById('onboarding')?.open")
    assert page.console == [], page.console


async def test_guided_setup_walks_one_step_at_a_time(page: Page) -> None:
    await page.evaluate("location.hash = 'setup'")
    await page.settle(2.0)
    await page.evaluate("document.querySelector('[data-action=\"onboarding-open\"]').click()")
    await page.settle(1.0)
    assert await page.evaluate("document.getElementById('onboarding').open")
    first = await page.evaluate("document.querySelector('#onboarding-title').innerText")
    eyebrow = await page.evaluate("document.querySelector('.onboarding-eyebrow').innerText")
    assert "of 5" in eyebrow.lower()
    await page.evaluate("Office.actions['onboarding-go']('0')")
    await page.settle(0.4)
    assert "Name your office" in await page.evaluate(
        "document.querySelector('#onboarding-title').innerText"
    )
    await page.evaluate("Office.actions['onboarding-go']('1')")
    await page.settle(0.4)
    assert (
        "model"
        in (await page.evaluate("document.querySelector('#onboarding-title').innerText")).lower()
    )
    await page.evaluate("document.querySelector('[data-action=\"onboarding-later\"]').click()")
    await page.settle(0.6)
    assert not await page.evaluate("document.getElementById('onboarding').open")
    assert first
    assert page.console == [], page.console
