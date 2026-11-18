"""A confirm step for anything that widens what the office may do (Settings, §10)."""

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


async def asked(page: Page) -> str:
    return str(
        await page.evaluate(
            "(() => { const d = document.getElementById('widen-confirm');"
            " return d?.open ? d.innerText : ''; })()"
        )
    )


async def answer(page: Page, choice: str) -> None:
    await page.evaluate(
        f"document.querySelector('#widen-confirm button[value=\"{choice}\"]').click()"
    )
    await page.settle(1.0)


async def browser_on(page: Page) -> bool:
    return bool(await page.evaluate("api('/settings').then((s) => s.settings.browser_on)"))


async def test_widening_asks_and_cancel_changes_nothing(page: Page) -> None:
    await page.evaluate(
        "api('/settings', {method: 'PUT', confirmed: true, body: {browser_on: false}})"
    )
    # Not awaited: the switch waits on the confirm, which the test answers.
    await page.evaluate("void Office.actions['browser-switch']('on'); 0")
    await page.settle(0.6)
    said = await asked(page)
    assert "This lets the office do more" in said and "browser" in said
    await answer(page, "cancel")
    assert await browser_on(page) is False, "cancelled: nothing was sent"
    assert page.console == [], page.console


async def test_confirm_widens_and_narrowing_never_asks(page: Page) -> None:
    # Not awaited: the switch waits on the confirm, which the test answers.
    await page.evaluate("void Office.actions['browser-switch']('on'); 0")
    await page.settle(0.6)
    await answer(page, "confirm")
    assert await browser_on(page) is True
    await page.evaluate("void Office.actions['browser-switch']('off'); 0")
    await page.settle(0.8)
    assert await asked(page) == "", "turning something off never asks"
    assert await browser_on(page) is False
    assert page.console == [], page.console


async def test_raising_a_limit_says_from_and_to(page: Page) -> None:
    await page.evaluate(
        "api('/settings', {method: 'PUT', confirmed: true, body: {run_max_cost_usd: 1}})"
    )
    await page.evaluate(
        "window.raised = api('/settings', {method: 'PUT', body: {run_max_cost_usd: 5}})"
        ".catch((e) => e.message); 0"
    )
    await page.settle(0.8)
    said = await asked(page)
    assert "What one task may spend" in said and "$1.00 → $5.00" in said, said
    await answer(page, "cancel")
    assert await page.evaluate("window.raised") == "not_confirmed"
    lowered = await page.evaluate(
        "api('/settings', {method: 'PUT', body: {run_max_cost_usd: 0.5}}).then(() => 'ok')"
    )
    assert lowered == "ok" and await asked(page) == "", "lowering a limit never asks"


async def test_the_no_project_tick_asks_and_cancel_leaves_it_off(page: Page) -> None:
    # Added whether or not it answers: the tick does not depend on that.
    server = await page.evaluate(
        "api('/mcp/servers', { method: 'POST', body: { name: 'No project test',"
        " url: 'https://example.com/mcp' } }).catch(() => null)"
        ".then(() => api('/mcp/servers'))"
        ".then((all) => all.find((s) => s.name === 'No project test')?.id)"
    )
    if not server:
        pytest.skip("the test server could not be added")
    await page.evaluate("location.hash = 'tools'")
    await page.settle(1.6)
    await page.evaluate(f"Office.actions['mcp-access']('{server}')")
    await page.settle(0.5)
    box = "document.querySelector('[data-mcp-no-project]')"
    assert await page.evaluate(f"{box}.checked") is False

    await page.evaluate(f"{box}.click()")
    assert "Any discussion with no project" in await asked(page)
    await answer(page, "cancel")
    assert await page.evaluate(f"{box}.checked") is False

    await page.evaluate(f"{box}.click()")
    await answer(page, "confirm")
    seen = await page.evaluate(
        f"api('/mcp/servers').then((all) => all.find((s) => s.id === '{server}')"
        ".no_project_discussions)"
    )
    assert seen is True
    assert page.console == [], page.console


async def test_a_routine_is_set_from_a_preset_and_asks_before_it_runs(page: Page) -> None:
    await page.evaluate("location.hash = 'routines'")
    await page.settle(1.6)
    assert await page.evaluate("Routines.presets.length") >= 3
    await page.evaluate("Office.actions['routine-new']('morning-recap')")
    await page.settle(0.5)
    await page.evaluate("document.querySelector('#modal form').requestSubmit()")
    assert "on its own" in await asked(page)
    await answer(page, "cancel")
    assert await page.evaluate("api('/routines').then((r) => r.routines.length)") == 0
    await page.evaluate("document.querySelector('#modal form').requestSubmit()")
    await answer(page, "confirm")
    await page.settle(1.0)
    seen = await page.evaluate("api('/routines').then((r) => r.routines[0])")
    assert seen["preset"] == "morning-recap" and seen["topic_id"]
    assert await page.evaluate("!!document.querySelector('[data-routine]')")
    assert page.console == [], page.console


async def test_the_to_dos_being_followed_show_on_the_routines_page(page: Page) -> None:
    await page.evaluate("location.hash = 'routines'")
    await page.settle(1.6)
    assert "To-dos being followed" in await page.evaluate("document.body.innerText")
    await page.evaluate(
        "Routines.todos.open = [{id: 'x', title: 'Pay the tax', due_on: '2026-11-02',"
        " status: 'open', progress: ''}]; Office.actions['todo-edit']('x')"
    )
    await page.settle(0.5)
    assert await page.evaluate("!!document.querySelector('#modal form [name=next_on]')")
    assert page.console == [], page.console


async def test_ai_history_searches_and_shows_nothing_found(page: Page) -> None:
    await page.evaluate("location.hash = 'archive'")
    await page.settle(1.6)
    assert "Import an export" in await page.evaluate("document.body.innerText")
    await page.evaluate(
        "const f = document.querySelector('form[data-form=archive-search]');"
        "f.q.value = 'nothing-like-this-was-said'; f.requestSubmit()"
    )
    await page.settle(1.0)
    assert "Nothing found" in await page.evaluate(
        "document.getElementById('archive-hits').innerText"
    )
    assert page.console == [], page.console


async def test_memories_are_reviewed_on_ai_history_and_reading_asks_first(page: Page) -> None:
    memory = {
        "id": "m1",
        "kind": "idea",
        "said_by": "ai",
        "content": "Take an EMI licence.",
        "corrected": False,
        "status": "proposed",
        "quote": "an EMI licence",
        "message_id": 7,
        "conversation": "archive:c1",
        "title": "Regulation",
        "source_name": "Claude",
        "at": None,
    }
    await page.evaluate(
        "window.realApi = api; api = (path, options) => path === '/archive' ? "
        "Promise.resolve({sources: [], imports: [], review: [], "
        f"memories: {{proposed: [{json.dumps(memory)}], kept: []}}}}) : realApi(path, options);"
        "location.hash = 'archive'"
    )
    await page.settle(1.6)
    row = await page.evaluate("document.querySelector('[data-memory=m1]')?.innerText || ''")
    assert "Take an EMI licence." in row and "An AI said" in row and "an EMI licence" in row
    await page.evaluate("Office.actions['archive-memory-edit']('m1')")
    await page.settle(0.4)
    assert (
        await page.evaluate(
            "document.querySelector('form[data-form=archive-memory-correct] textarea')?.value"
        )
        == "Take an EMI licence."
    )
    await page.evaluate("closeModal(); api = realApi")
    # Not awaited: the call waits on the confirm, which the test answers.
    await page.evaluate(
        "void Office.actions['archive-read']('00000000-0000-0000-0000-000000000000'); 0"
    )
    await page.settle(0.6)
    assert "lead's model" in await asked(page)
    await answer(page, "cancel")
    assert page.console == [], page.console


async def test_ai_history_settings_ask_only_when_they_widen(page: Page) -> None:
    await page.evaluate("location.hash = 'archive'")
    await page.settle(1.6)
    assert await page.evaluate("!!document.querySelector('form[data-form=archive-export]')")
    await page.evaluate(
        "const f = document.querySelector('form[data-form=archive-protection]');"
        "f.agents.value = 'off'; f.requestSubmit()"
    )
    await page.settle(1.0)
    assert await asked(page) == "", "narrowing does not ask"
    await page.evaluate(
        "const g = document.querySelector('form[data-form=archive-protection]');"
        "g.agents.value = 'discussions_and_slack'; void g.requestSubmit(); 0"
    )
    await page.settle(0.6)
    assert "AI history can reach more" in await asked(page)
    await answer(page, "cancel")
    assert page.console == [], page.console


async def test_help_opens_from_the_keys_and_the_sidebar(page: Page) -> None:
    await page.evaluate(
        "document.dispatchEvent("
        "new KeyboardEvent('keydown', {key: '/', ctrlKey: true, bubbles: true}))"
    )
    await page.settle(0.6)
    shown = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "Keep an idea" in shown and "Stop the work running in the thread" in shown
    await page.evaluate("closeModal()")
    await page.evaluate("document.querySelector('[data-action=\"help-open\"]').click()")
    await page.settle(0.6)
    assert await page.evaluate("!!document.querySelector('#modal-content .help-panel')")
    assert page.console == [], page.console


async def test_text_size_scales_the_page_and_is_kept(page: Page) -> None:
    await page.evaluate("Office.actions['text-size']('1.15')")
    await page.settle(0.8)
    assert await page.evaluate("getComputedStyle(document.documentElement).fontSize") == "18.4px"
    await page.evaluate("location.reload()")
    await page.settle(3.0)
    assert await page.evaluate("getComputedStyle(document.documentElement).fontSize") == "18.4px"
    await page.evaluate("Office.actions['text-size']('1')")
    await page.settle(0.6)
    assert await page.evaluate("getComputedStyle(document.documentElement).fontSize") == "16px"
    assert page.console == [], page.console
