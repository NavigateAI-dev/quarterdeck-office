"""The owner's verdict on the lead's answers, in a real browser."""

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


async def open_answer(page: Page, office: dict[str, Any]) -> str:
    await page.evaluate(f"location.hash = 'topic/{office['asked_topic']}'")
    await page.settle(3.0)
    return f'[data-verdict-for="{office["answer"]}"]'


async def test_a_reply_takes_a_thumbs_up(page: Page, office: dict[str, Any]) -> None:
    row = await open_answer(page, office)
    assert await page.evaluate(f"!!document.querySelector('{row}')"), "the reply has a verdict row"
    await page.evaluate(f"document.querySelector('{row} .verdict-btn').click()")
    await page.settle(1.2)
    pressed = await page.evaluate(
        f"document.querySelector('{row} .verdict-btn').getAttribute('aria-pressed')"
    )
    assert pressed == "true"
    assert page.console == [], page.console


async def test_a_thumbs_down_asks_why_and_offers_the_full_team(
    page: Page, office: dict[str, Any]
) -> None:
    row = await open_answer(page, office)
    await page.evaluate(f"document.querySelectorAll('{row} .verdict-btn')[1].click()")
    await page.settle(0.6)
    assert "What was wrong?" in await page.evaluate(
        "document.querySelector('#modal-content')?.innerText || ''"
    )
    await page.evaluate(
        "document.querySelector('#verdict-why-text').value = 'No source';"
        "document.querySelector('#verdict-why').requestSubmit()"
    )
    await page.settle(1.2)
    shown = await page.evaluate(f"document.querySelector('{row}')?.innerText || ''")
    assert "No source" in shown and "Redo with full team" in shown
    assert page.console == [], page.console


async def test_the_lead_on_real_use_is_in_performance(page: Page) -> None:
    await page.evaluate("Settings.tab = 'performance'; location.hash = 'settings'")
    await page.settle(3.0)
    card = await page.evaluate("document.querySelector('#lead-quality')?.innerText || ''")
    assert "The lead" in card and "The full team" in card and "Your verdicts" in card
    assert page.console == [], page.console


async def test_the_calls_behind_a_reply_open_from_it(page: Page, office: dict[str, Any]) -> None:
    await open_answer(page, office)
    button = f'[data-mid="m:{office["answer"]}"] [data-action^="room-calls"]'
    assert await page.evaluate(f"!!document.querySelector('{button}')"), "an agent reply offers it"
    await page.evaluate(f"document.querySelector('{button}').click()")
    await page.settle(1.2)
    listed = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "How this reply was made" in listed and "seeded-model" in listed
    first = '#modal-content [data-action^="model-call:"]'
    await page.evaluate(f"document.querySelector('{first}').click()")
    await page.settle(1.2)
    opened = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "21%, the standard rate." in opened
