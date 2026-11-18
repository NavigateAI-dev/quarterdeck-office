"""The idea wall in three kinds, in a real browser, on a throwaway office."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

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


async def wall(page: Page, view: str = "kind") -> None:
    """The Ideas widget wide on the floor, in the given view, no kind chosen."""
    await page.evaluate("api('/widgets/ideas', {method: 'PATCH', body: {size: 'wide'}})")
    await page.evaluate(
        f"localStorage.setItem('office.ideas.view', {json.dumps(view)});"
        " localStorage.removeItem('office.idea.kind');"
        f" Ideas.view = {json.dumps(view)}; Ideas.pick = ''; location.hash = 'home'; rerender()"
    )
    await page.settle(2.0)


async def column_of(page: Page, words: str) -> str:
    return str(
        await page.evaluate(
            "[...document.querySelectorAll('.idea-board [data-idea]')]"
            f".find((c) => c.innerText.includes({json.dumps(words)}))"
            "?.closest('.idea-col, .idea-unsorted')?.ariaLabel || ''"
        )
    )


async def keep(page: Page, words: str) -> None:
    await page.evaluate(
        "(() => { const box = document.querySelector('#idea-text');"
        f" box.value = {json.dumps(words)}; box.form.requestSubmit(); }})()"
    )
    await page.settle(1.8)


async def test_the_wall_has_a_column_for_each_kind(page: Page) -> None:
    await wall(page)
    columns = await page.evaluate(
        "[...document.querySelectorAll('.idea-kind-col')].map((c) => c.ariaLabel)"
    )
    assert columns == ["Friction", "Curiosity", "Opportunity"]
    asks = await page.evaluate(
        "[...document.querySelectorAll('.idea-kind-ask')].map((p) => p.innerText).join(' | ')"
    )
    assert "tedious" in asks and "genuinely curious" in asks and "pay to solve" in asks
    assert page.console == [], page.console


async def test_a_thought_kept_with_a_kind_lands_in_its_column(page: Page) -> None:
    await wall(page)
    await page.evaluate(
        "document.querySelector('[data-action=\"idea-kind-pick:curiosity\"]').click()"
    )
    ask = await page.evaluate("document.querySelector('.idea-board [data-idea-ask]').innerText")
    assert "genuinely curious" in ask, "the chosen kind asks its question"
    words = f"Local-first sync engines {uuid4().hex[:6]}"
    await keep(page, words)
    assert await column_of(page, words) == "Curiosity"
    three = await page.evaluate("document.querySelector('.idea-board .idea-three')?.ariaLabel")
    assert three.startswith("Today's three:") and "0 of 3" not in three
    on = await page.evaluate(
        "document.querySelector('.idea-board .idea-three-mark.is-curiosity')"
        ".classList.contains('on')"
    )
    assert on, "today's curiosity is ticked"
    assert page.console == [], page.console


async def test_an_unsorted_idea_takes_a_kind_with_one_tap(page: Page) -> None:
    await wall(page)
    words = f"An old thought {uuid4().hex[:6]}"
    await keep(page, words)
    assert await column_of(page, words) == "Unsorted"
    idea = await page.evaluate(
        "(async () => (await api('/ideas')).kinds.unsorted"
        f".find((i) => i.text === {json.dumps(words)}).id)()"
    )
    await page.evaluate(
        f"document.querySelector('[data-action=\"idea-kind:{idea}:opportunity\"]').click()"
    )
    await page.settle(1.8)
    assert await column_of(page, words) == "Opportunity"
    assert page.console == [], page.console


async def test_the_board_by_day_is_one_tap_away_and_remembered(page: Page) -> None:
    await wall(page)
    await page.evaluate("document.querySelector('[data-action=\"ideas-view:day\"]').click()")
    await page.settle(1.6)
    assert await page.evaluate("document.querySelector('.idea-board').dataset.view") == "day"
    assert await page.evaluate("!!document.querySelector('.idea-board .idea-col-new')")
    await page.evaluate("location.reload()")
    await page.settle(3.0)
    assert await page.evaluate("document.querySelector('.idea-board')?.dataset.view") == "day"
    await page.evaluate("document.querySelector('[data-action=\"ideas-view:kind\"]').click()")
    await page.settle(1.6)
    assert await page.evaluate("document.querySelector('.idea-board').dataset.view") == "kind"
    assert page.console == [], page.console


async def test_on_a_phone_the_kinds_fit_and_the_idea_screen_has_them(page: Page) -> None:
    await page.viewport(390, 844, 3)
    await wall(page)
    assert await page.evaluate("document.documentElement.scrollWidth <= 390") is True
    await page.evaluate("location.hash = 'idea'")
    await page.settle(2.0)
    chips = await page.evaluate("document.querySelectorAll('#idea-screen .idea-kind-chip').length")
    assert chips == 3
    assert await page.evaluate("document.documentElement.scrollWidth <= 390") is True
    assert page.console == [], page.console
