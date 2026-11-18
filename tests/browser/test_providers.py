"""Provider cards and failover, in a real browser, on a throwaway office."""

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
        yield open_page


async def models_tab(page: Page) -> None:
    await page.evaluate("Settings.tab = 'models'; location.hash = 'settings'")
    await page.settle(2.5)


async def test_each_provider_has_a_card(page: Page, office: dict[str, Any]) -> None:
    await models_tab(page)
    cards = json.loads(
        await page.evaluate(
            "JSON.stringify([...document.querySelectorAll('.pv-card')].map((c) =>"
            " ({key: c.dataset.provider, text: c.innerText})))"
        )
    )
    keys = [card["key"] for card in cards]
    assert office["deepseek"] in keys and office["haiku"] in keys, keys
    assert "gateway" not in keys, "the gateway is retired"
    deepseek = next(c for c in cards if c["key"] == office["deepseek"])
    assert "No backup yet" in deepseek["text"]
    banner = await page.evaluate("document.querySelector('.pv-moved')?.innerText || ''")
    assert "No model yet for" in banner and "Chief" in banner, "roles without a model are named"
    summary = await page.evaluate("document.querySelector('.pv-summary')?.innerText || ''")
    assert "Spent this month" in summary and "Failed over this week" in summary
    assert await page.evaluate("!!document.getElementById('role-models-form')")
    assert page.console == [], page.console


async def test_a_backup_is_chosen_from_a_card(page: Page, office: dict[str, Any]) -> None:
    await models_tab(page)
    key = office["deepseek"]
    await page.evaluate(
        f"document.querySelector('[data-action=\"provider-backup:{key}\"]').click()"
    )
    await page.settle(0.8)
    dialog = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "When DeepSeek fails" in dialog and "never raises what a task may spend" in dialog
    haiku = office["haiku"]
    await page.evaluate(
        f"document.querySelector('#provider-backup-form input[value=\"{haiku}\"]').click();"
        "document.querySelector('#provider-backup-form button.primary').click()"
    )
    await page.settle(2.5)
    card = await page.evaluate(
        f"document.querySelector('.pv-card[data-provider=\"{key}\"]')?.innerText || ''"
    )
    assert "Haiku" in card and "Change backup" in card, card
    assert page.console == [], page.console


async def test_a_task_that_moved_says_so(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate("localStorage.setItem('office.editor.open', '0')")
    await page.evaluate(f"location.hash = 'task/{office['brief']}'")
    await page.settle(3.0)
    note = await page.evaluate("document.querySelector('[data-failover]')?.innerText || ''")
    assert "Moved to your backup" in note and "Haiku" in note
    assert "ran out of credit" in note and "budget and limits are unchanged" in note
    assert page.console == [], page.console


async def test_on_a_phone_the_cards_stack(page: Page) -> None:
    await page.viewport(390, 844, 3)
    await models_tab(page)
    assert await page.evaluate("document.documentElement.scrollWidth <= 390") is True
    assert page.console == [], page.console


async def test_usage_is_shown_by_provider_over_a_chosen_period(page: Page) -> None:
    await models_tab(page)
    section = await page.evaluate("document.getElementById('provider-usage')?.innerText || ''")
    assert "Usage by provider" in section and "Cost by day" in section
    assert "ran out of credit" in section, "the seeded failover is in what happened"
    await page.evaluate("document.querySelector('[data-action=\"provider-days:30\"]').click()")
    await page.settle(2.0)
    pressed = await page.evaluate(
        "document.querySelector('[data-action=\"provider-days:30\"]')?.getAttribute('aria-pressed')"
    )
    assert pressed == "true"
    assert page.console == [], page.console
