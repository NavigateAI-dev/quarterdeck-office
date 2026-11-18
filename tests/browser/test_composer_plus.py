"""The thread's input bar, learned from DSH's, in a real browser."""

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


async def open_thread(page: Page, words: str = "Pricing for the new plans") -> str:
    topic = await page.evaluate(
        "(async () => (await api('/topics', {method: 'POST', body: {text: "
        f"{json.dumps(words)}}}}})).topic.id)()"
    )
    await page.evaluate(f"location.hash = 'topic/{topic}'")
    await page.settle(3.0)
    return str(topic)


async def menu_open(page: Page) -> bool:
    return bool(await page.evaluate("!document.querySelector('#composer-menu').hidden"))


async def test_the_plus_menu_adds_and_runs_commands(page: Page) -> None:
    await open_thread(page)
    await page.evaluate("document.querySelector('.composer-plus').click()")
    await page.settle(0.4)
    assert await menu_open(page)
    names = await page.evaluate(
        "[...document.querySelectorAll('#composer-menu .composer-menu-name')]"
        ".map((n) => n.innerText)"
    )
    assert names == [
        "File",
        "Folder",
        "From your library",
        "Goal",
        "Plan",
        "Model",
        "Access",
        "Export",
    ]
    groups = await page.evaluate(
        "[...document.querySelectorAll('#composer-menu .composer-menu-group')]"
        ".map((g) => g.innerText)"
    )
    assert groups == ["Add", "Commands"]
    await page.evaluate(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
    )
    await page.settle(0.3)
    assert not await menu_open(page), "Esc closes the menu"
    assert await page.evaluate("document.activeElement?.id") == "room-text"
    await page.evaluate(
        "document.querySelector('#room-text').dispatchEvent("
        "new KeyboardEvent('keydown', {key: '/', bubbles: true}))"
    )
    await page.settle(0.3)
    assert await menu_open(page), "/ in an empty box opens it too"
    assert page.console == [], page.console


async def test_access_offers_read_or_work_and_never_full(page: Page) -> None:
    await open_thread(page)
    chip = await page.evaluate("document.querySelector('#composer-access')?.innerText || ''")
    assert "Read only" in chip
    await page.evaluate("document.querySelector('#composer-access').click()")
    await page.settle(0.4)
    pop = await page.evaluate("document.querySelector('#composer-pop').innerText")
    assert "Read only" in pop and "Work on a branch" in pop and "no full access" in pop
    off = await page.evaluate(
        "document.querySelector('[data-action=\"composer-access:work\"]').getAttribute('aria-disabled')"
    )
    assert off == "true", "work is off until the office has a writable folder"
    assert page.console == [], page.console


async def test_a_goal_is_set_from_the_menu_and_pinned_above_the_box(page: Page) -> None:
    topic = await open_thread(page)
    await page.evaluate("document.querySelector('.composer-plus').click()")
    await page.settle(0.3)
    await page.evaluate("document.querySelector('[data-action=\"composer-goal\"]').click()")
    await page.settle(0.5)
    await page.evaluate(
        "document.querySelector('#composer-goal-text').value = 'Decide the launch price';"
        "document.querySelector('#composer-goal-cap').value = '2';"
        "document.querySelector('#composer-goal-form').requestSubmit()"
    )
    # A limit above the usual $0.50 widens what work may spend: it asks first (13a).
    await page.settle(0.6)
    await page.evaluate(
        "document.querySelector('#widen-confirm button[value=\"confirm\"]').click()"
    )
    await page.settle(1.5)
    pinned = await page.evaluate("document.querySelector('#composer-goal')?.innerText || ''")
    assert "Decide the launch price" in pinned and "$2.00" in pinned
    saved = await page.evaluate(f"(async () => (await api('/topics/{topic}/settings')).goal)()")
    assert saved == "Decide the launch price"
    assert page.console == [], page.console


async def test_plan_first_marks_the_next_message_only(page: Page) -> None:
    topic = await open_thread(page)
    await page.evaluate("document.querySelector('.composer-plus').click()")
    await page.settle(0.3)
    await page.evaluate("document.querySelector('[data-action=\"composer-plan\"]').click()")
    await page.settle(0.4)
    assert "Plan first" in await page.evaluate("document.querySelector('#composer-goal').innerText")
    await page.evaluate(
        "document.querySelector('#room-text').value = 'Redo the pricing page';"
        "document.querySelector('#room-form').requestSubmit()"
    )
    await page.settle(2.5)
    assert not await page.evaluate("ComposerPlus.plan"), "only the next message"
    assert await page.evaluate("document.querySelector('#composer-goal').hidden")
    sent = await page.evaluate(
        f"(async () => (await api('/topics/{topic}/feed?after_event=0&after_message=0'))"
        ".items.filter((m) => m.kind === 'message' && m.author?.kind === 'owner')"
        ".map((m) => m.actions || []))()"
    )
    assert any(a.get("kind") == "plan_first" for a in sent[-1]), sent
    assert page.console == [], page.console


async def test_the_status_line_and_export(page: Page) -> None:
    topic = await open_thread(page)
    line = await page.evaluate("document.querySelector('#composer-status')?.innerText || ''")
    assert "turn" in line and "tok" in line
    got = await page.evaluate(f"(async () => (await fetch('/topics/{topic}/export.zip')).status)()")
    assert got == 200
    assert page.console == [], page.console


async def test_on_a_phone_the_bar_fits(page: Page) -> None:
    await page.viewport(390, 844, 3)
    await open_thread(page)
    assert await page.evaluate("document.documentElement.scrollWidth <= 390") is True
    assert await page.evaluate("!!document.querySelector('#composer-model')")
    assert page.console == [], page.console
