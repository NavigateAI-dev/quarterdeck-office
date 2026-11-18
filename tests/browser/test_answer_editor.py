"""The answer in an editor beside the task, in a real browser.

These change data, so they run against a throwaway office of their own
(`seeded_office.py`, in its own schema), never the owner's: they need
TEST_DATABASE_URL as the integration suites do.
"""

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
        pytest.skip("Set TEST_DATABASE_URL to run the editor against a throwaway office")
    if find_browser() is None:
        pytest.skip("no browser installed")
    with running_office() as started:
        yield started


@pytest.fixture
async def page(office: dict[str, Any]) -> AsyncIterator[Page]:
    async with browser(sign_in_url(office)) as open_page:
        await open_page.settle(2.0)
        yield open_page


async def open_task(page: Page, run_id: str) -> None:
    await page.evaluate(f"location.hash = 'task/{run_id}'")
    await page.settle(3.0)


async def answer_of(page: Page, run_id: str) -> dict[str, Any]:
    raw = await page.evaluate(
        f"(async () => JSON.stringify(await (await fetch('/runs/{run_id}/answer',"
        " {credentials: 'same-origin'})).json()))()"
    )
    return dict(json.loads(raw))


async def test_a_written_answer_opens_beside_the_task_and_closes_to_a_strip(
    page: Page, office: dict[str, Any]
) -> None:
    await open_task(page, office["brief"])
    shown = await page.evaluate(
        "JSON.stringify({open: !document.querySelector('#answer-editor').hidden,"
        " title: document.querySelector('#answer-editor .ed-page h1')?.textContent || '',"
        " card: document.querySelector('[data-answer-place]')?.innerText || '',"
        " shifted: document.body.classList.contains('editor-open')})"
    )
    state = json.loads(shown)
    assert state["open"], "the answer did not open beside the task"
    assert state["title"] == "Client onboarding against the Wwft"
    assert "open on the right" in state["card"]
    assert state["shifted"], "the task page was not moved aside"
    await page.evaluate("document.querySelector('#answer-editor [data-ed=close]').click()")
    await page.settle(0.5)
    closed = await page.evaluate(
        "JSON.stringify({panel: document.querySelector('#answer-editor').hidden,"
        " rail: !document.querySelector('#answer-editor-rail').hidden,"
        " card: document.querySelector('[data-answer-place]')?.innerText || ''})"
    )
    state = json.loads(closed)
    assert state["panel"] and state["rail"], state
    assert "Edit in the editor" in state["card"], "closed, the room shows the answer itself"
    assert "Where we fall short" in state["card"]
    # The owner's choice is remembered: the next task opens closed.
    await open_task(page, office["deck"])
    assert await page.evaluate("document.querySelector('#answer-editor').hidden") is True
    await page.evaluate("document.querySelector('#answer-editor-rail').click()")
    await page.settle(0.5)
    assert await page.evaluate("!document.querySelector('#answer-editor').hidden") is True
    assert page.console == [], f"the editor reported errors: {page.console}"


async def test_an_edit_is_saved_as_the_owners_version_and_survives_a_reload(
    page: Page, office: dict[str, Any]
) -> None:
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await open_task(page, office["brief"])
    await page.evaluate(
        """(() => {
          const point = document.querySelector('#answer-editor .ed-page li');
          const range = document.createRange();
          range.selectNodeContents(point);
          range.collapse(false);
          const selection = getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.querySelector('#answer-editor .ed-page').focus();
          document.execCommand('insertText', false, ' Checked by the owner.');
        })()"""
    )
    await page.evaluate("AnswerEditor.save()")
    await page.settle(2.0)
    answer = await answer_of(page, office["brief"])
    assert answer["current"]["by"] == "owner", answer["current"]
    assert answer["current"]["version"] == 2
    await page.evaluate("location.reload()")
    await page.settle(3.5)
    text = await page.evaluate("document.querySelector('#answer-editor .ed-page')?.innerText || ''")
    assert "Checked by the owner." in text, text[:300]
    downloads = await page.evaluate(
        "[...document.querySelectorAll('#answer-editor .ed-download-menu a')]"
        ".map((a) => a.href).join(' ')"
    )
    assert answer["current"]["artifact_id"] in downloads, "downloads must carry the edit"
    assert page.console == [], f"the editor reported errors: {page.console}"


async def test_a_deck_edits_slide_by_slide(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await open_task(page, office["deck"])
    count = await page.evaluate("document.querySelectorAll('#answer-editor .ed-thumb').length")
    assert count == 3, f"slides shown: {count}"
    await page.evaluate("document.querySelector('#answer-editor [data-ed=slide-add]').click()")
    await page.settle(0.3)
    await page.evaluate(
        """(() => {
          const heading = document.querySelector('#answer-editor .ed-slide h2');
          heading.textContent = 'What it costs';
          heading.dispatchEvent(new InputEvent('input', {bubbles: true}));
        })()"""
    )
    await page.evaluate("AnswerEditor.save()")
    await page.settle(2.0)
    await page.evaluate("location.reload()")
    await page.settle(3.5)
    headings = await page.evaluate(
        "[...document.querySelectorAll('#answer-editor .ed-thumb strong')]"
        ".map((n) => n.textContent).join('|')"
    )
    assert headings.split("|")[1] == "What it costs", headings
    assert len(headings.split("|")) == 4
    assert page.console == [], f"the editor reported errors: {page.console}"


async def test_a_selection_offers_to_ask_and_to_rewrite(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await open_task(page, office["brief"])
    await page.evaluate(
        """(() => {
          const point = document.querySelector('#answer-editor .ed-page li');
          const range = document.createRange();
          range.selectNodeContents(point);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
        })()"""
    )
    await page.settle(0.4)
    bubble = await page.evaluate(
        "(() => { const b = document.getElementById('ed-bubble');"
        " return b && !b.hidden ? b.innerText : ''; })()"
    )
    assert "Ask about this" in bubble and "Ask for a rewrite" in bubble, bubble
    await page.evaluate("document.querySelector('[data-ed-bubble=rewrite]').click()")
    await page.settle(0.3)
    form = await page.evaluate(
        "document.querySelector('#answer-editor [data-ed-askform]')?.innerText || ''"
    )
    assert "one paid call" in form, "a rewrite must say it spends before it is pressed"
    assert page.console == [], f"the editor reported errors: {page.console}"


async def test_on_a_phone_the_editor_is_the_whole_screen(
    page: Page, office: dict[str, Any]
) -> None:
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await page.viewport(390, 844, 3)
    await open_task(page, office["brief"])
    width = await page.evaluate(
        "Math.round(document.querySelector('#answer-editor').getBoundingClientRect().width)"
    )
    assert width == 390, f"panel width on a phone: {width}"
    assert await page.evaluate("document.documentElement.scrollWidth <= 390") is True
    assert page.console == [], f"the editor reported errors: {page.console}"


async def test_the_side_menu_and_header_stay_while_the_page_scrolls(
    page: Page, office: dict[str, Any]
) -> None:
    """UX1: the side menu scrolled away with the page, and the status pill sat
    on the breadcrumb once the editor narrowed the page."""
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await page.viewport(1440, 900)
    await open_task(page, office["brief"])
    await page.evaluate("window.scrollTo(0, 700)")
    await page.settle(0.5)
    measured = json.loads(
        await page.evaluate(
            """JSON.stringify((() => {
              const aside = document.querySelector('.shell > aside').getBoundingClientRect();
              const header = document.querySelector('main > header').getBoundingClientRect();
              const pill = document.getElementById('island').getBoundingClientRect();
              const trail = document.getElementById('breadcrumb').getBoundingClientRect();
              const logo = getComputedStyle(document.querySelector('.brand .logo')).fontSize;
              return {scrolled: scrollY, aside: aside.top, height: aside.height,
                      header: header.top, clear: pill.width === 0 || pill.left >= trail.right,
                      rail: aside.width, logo};
            })())"""
        )
    )
    assert measured["scrolled"] > 0, "the page did not scroll"
    # UI-1: the sidebar stays down the left, the whole height, while the page scrolls.
    assert measured["aside"] == 0 and measured["height"] >= 880, measured
    assert measured["header"] == 0, "the header scrolled away"
    assert measured["clear"], "the status pill covers the breadcrumb"
    assert measured["logo"] != "0px", "the bar's mark lost its letters"
    spill = await page.evaluate(
        "(() => { const bar = document.querySelector('.shell > aside').getBoundingClientRect();"
        " return [...document.querySelectorAll('#nav a.side-row')]"
        ".filter((a) => a.getBoundingClientRect().right > bar.right + 1).length; })()"
    )
    assert spill == 0, "beside the editor every place still fits the rail"
    labels = await page.evaluate(
        "[...document.querySelectorAll('nav a')].filter((a) => a.offsetParent !== null)"
        ".every((a) => a.getAttribute('aria-label') || a.getAttribute('title'))"
    )
    assert labels is True, "an icon without a name"
    assert page.console == [], page.console


async def select_in(page: Page, selector: str, nth: int = 0) -> None:
    await page.evaluate(
        f"""(() => {{
          const node = document.querySelectorAll('{selector}')[{nth}];
          const range = document.createRange();
          range.selectNodeContents(node);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          document.querySelector('#answer-editor [data-ed-page]').focus();
        }})()"""
    )


async def test_underline_and_a_link_survive_a_save(page: Page, office: dict[str, Any]) -> None:
    """ED1."""
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await open_task(page, office["brief"])
    await select_in(page, "#answer-editor .ed-page li", 0)
    await page.evaluate("document.execCommand('underline'); AnswerEditor.changed()")
    await select_in(page, "#answer-editor .ed-page li", 1)
    await page.evaluate(
        "document.execCommand('createLink', false, 'https://example.com/ubo');"
        " AnswerEditor.changed()"
    )
    await page.evaluate("AnswerEditor.save()")
    await page.settle(2.0)
    await page.evaluate("location.reload()")
    await page.settle(3.5)
    html = await page.evaluate("document.querySelector('#answer-editor .ed-page')?.innerHTML || ''")
    assert "<u>" in html, html[:400]
    assert 'href="https://example.com/ubo"' in html, html[:400]
    assert page.console == [], page.console


async def test_speaker_notes_survive_a_save(page: Page, office: dict[str, Any]) -> None:
    """ED2."""
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await open_task(page, office["deck"])
    await page.evaluate(
        """(() => {
          const notes = document.querySelector('#answer-editor [data-ed-notes]');
          notes.value = 'Open with the two clients.';
          notes.dispatchEvent(new Event('input', {bubbles: true}));
        })()"""
    )
    await page.evaluate("AnswerEditor.save()")
    await page.settle(2.0)
    await page.evaluate("location.reload()")
    await page.settle(3.5)
    kept = await page.evaluate(
        "document.querySelector('#answer-editor [data-ed-notes]')?.value || ''"
    )
    assert kept == "Open with the two clients.", kept
    assert page.console == [], page.console


async def test_an_earlier_version_can_be_the_answer_again(
    page: Page, office: dict[str, Any]
) -> None:
    """ED3: restoring is a new version; nothing earlier is lost."""
    await page.evaluate("localStorage.removeItem('office.editor.open')")
    await open_task(page, office["brief"])
    before = await answer_of(page, office["brief"])
    assert before["current"]["version"] >= 2, "an earlier test edited the brief"
    await page.evaluate(
        "document.querySelector('#answer-editor [data-ed=\"view:versions\"]').click()"
    )
    await page.settle(0.6)
    await page.evaluate(
        "[...document.querySelectorAll('#answer-editor [data-ed^=\"restore:\"]')]"
        ".find((b) => b.dataset.ed.endsWith(':1')).click()"
    )
    await page.settle(3.0)
    after = await answer_of(page, office["brief"])
    assert after["current"]["version"] == before["current"]["version"] + 1
    assert after["current"]["by"] == "owner"
    text = await page.evaluate("document.querySelector('#answer-editor .ed-page')?.innerText || ''")
    assert "Checked by the owner." not in text, "the first version's words are back"
    assert len(after["versions"]) == len(before["versions"]) + 1, "nothing is lost"
    assert page.console == [], page.console


async def test_a_long_reply_can_be_copied_and_opened_in_the_editor(
    page: Page, office: dict[str, Any]
) -> None:
    """A long reply in a discussion: Copy, and Open in editor as a document."""
    await page.evaluate(f"location.hash = 'topic/{office['topic']}'")
    await page.settle(3.0)
    tools = await page.evaluate(
        "[...document.querySelector('[data-action^=\"room-open-doc:\"]').parentElement.children]"
        ".map((b) => b.getAttribute('aria-label'))"
    )
    assert "Copy" in tools and "Open in editor" in tools, tools
    await page.evaluate(
        "window.copied = null;"
        "navigator.clipboard.writeText = async (text) => { window.copied = text; };"
        "document.querySelector('[data-action^=\"room-copy:\"]').click()"
    )
    await page.settle(0.5)
    copied = await page.evaluate("window.copied || ''")
    assert copied.startswith("Using general knowledge") and "do not sell" in copied
    await page.evaluate("document.querySelector('[data-action^=\"room-open-doc:\"]').click()")
    await page.settle(3.0)
    shown = json.loads(
        await page.evaluate(
            "JSON.stringify({open: !document.querySelector('#answer-editor').hidden,"
            " title: document.querySelector('#answer-editor .ed-title h2')?.textContent,"
            " headings: [...document.querySelectorAll('#answer-editor .ed-page h2')]"
            ".map((h) => h.textContent)})"
        )
    )
    assert shown["open"] and shown["title"] == "The document", shown
    assert "Privacy Policy" in shown["headings"], shown
    await select_in(page, "#answer-editor .ed-page p", 0)
    await page.evaluate("document.execCommand('bold'); AnswerEditor.changed(); AnswerEditor.save()")
    await page.settle(2.0)
    versions = await page.evaluate("AnswerEditor.answer.versions.length")
    assert versions == 2, "the edit is the document's second version"
    await page.evaluate("location.hash = 'home'")
    await page.settle(1.5)
    assert await page.evaluate("document.querySelector('#answer-editor').hidden") is True
    assert page.console == [], page.console
