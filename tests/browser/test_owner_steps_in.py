"""Stepping into a running task, in a real browser.

A stuck task asks for help on its page; the message bar sits near the top;
a step can be skipped from the plan; an agent's proposed tool call waits for
the owner's press. Read only: nothing here presses a button that runs work.
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
        pytest.skip("Set TEST_DATABASE_URL to run against a throwaway office")
    if find_browser() is None:
        pytest.skip("no browser installed")
    with running_office() as started:
        yield started


@pytest.fixture
async def page(office: dict[str, Any]) -> AsyncIterator[Page]:
    async with browser(sign_in_url(office)) as open_page:
        await open_page.settle(2.0)
        yield open_page


async def test_a_stuck_task_asks_for_help_in_words(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate(f"location.hash = 'task/{office['stuck']}'")
    await page.settle(3.0)
    card = str(await page.evaluate("document.querySelector('[data-stuck]')?.innerText || ''"))
    assert "Stuck: it needs your help" in card, card[:300]
    assert "click 'Case studies' on www.loot-drop.io 2 times" in card
    assert "the page did not respond in time" in card
    for label in ("Send hint and continue", "Skip this part", "Finish with what it has"):
        assert label in card, f"{label} is missing"
    assert page.console == [], page.console


async def test_the_message_bar_sits_above_the_work(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate(f"location.hash = 'task/{office['stuck']}'")
    await page.settle(3.0)
    placed = json.loads(
        await page.evaluate(
            "JSON.stringify({bar: document.querySelector('.message-bar')"
            "?.getBoundingClientRect().top ?? -1,"
            " work: document.querySelector('.task-work')?.getBoundingClientRect().top ?? -1,"
            " input: !!document.querySelector('.message-bar input[name=text]')})"
        )
    )
    assert placed["input"], "no message box on a paused task"
    assert 0 <= placed["bar"] < placed["work"], placed


async def test_a_waiting_step_can_be_skipped_from_the_plan(
    page: Page, office: dict[str, Any]
) -> None:
    await page.evaluate(f"location.hash = 'task/{office['stuck']}'")
    await page.settle(3.0)
    await page.evaluate("document.querySelector('[data-action=\"tab:plan\"]')?.click()")
    await page.settle(1.2)
    skips = await page.evaluate("document.querySelectorAll('[data-action^=\"step-skip:\"]').length")
    assert skips == 2
    assert "Browse" in str(await page.evaluate("document.querySelector('.plan-steps')?.innerText"))


async def test_a_proposed_tool_call_waits_for_the_owners_press(
    page: Page, office: dict[str, Any]
) -> None:
    await page.evaluate(f"location.hash = 'topic/{office['topic']}'")
    await page.settle(3.0)
    feed = str(await page.evaluate("document.querySelector('#content')?.innerText || ''"))
    assert "Used Miro · board_search_boards" in feed
    assert "board_create on Miro" in feed and "Allow this call" in feed
    assert page.console == [], page.console


async def test_a_build_shows_what_it_made(page: Page, office: dict[str, Any]) -> None:
    """B5: built, its files, a sandboxed preview and a download."""
    await page.evaluate(f"location.hash = 'task/{office['built']}'")
    await page.settle(3.0)
    card = str(await page.evaluate("document.querySelector('#build-card')?.innerText || ''"))
    assert "Built" in card and "index.html" in card and "README.md" in card, card[:300]
    links = json.loads(
        await page.evaluate(
            "JSON.stringify([...document.querySelectorAll('#build-card a')]"
            ".map(a => a.getAttribute('href')))"
        )
    )
    assert any(link.startswith("/previews/") for link in links), links
    assert any("/build.zip" in link for link in links), links
    # This task's version, every version, and the next one.
    assert "Version 1" in card and "the project is now at version 2" in card, card[:400]
    assert "All versions (2)" in card and "Make version 3" in card
    assert await page.evaluate("!!document.querySelector('[data-action^=\"build-restore:\"]')")
    assert page.console == [], page.console


async def test_a_builds_next_step_is_a_version_not_a_report(
    page: Page, office: dict[str, Any]
) -> None:
    """The owner's report: Change something on a website offered report shapes."""
    await page.evaluate(f"location.hash = 'task/{office['built']}'")
    await page.settle(3.0)
    actions = str(await page.evaluate("document.querySelector('#content')?.innerText || ''"))
    assert "Make the next version" in actions and "Ask a follow-up" not in actions
    words = await page.evaluate(
        "[...document.querySelectorAll('.card.answer a.button')].map(a => a.innerText)"
    )
    assert "Word" not in words and "Slides" not in words, words
    await page.evaluate("Office.actions['follow-up']()")
    await page.settle(0.8)
    dialog = str(await page.evaluate("document.querySelector('#modal-content')?.innerText || ''"))
    assert "Make version" in dialog and "What should change?" in dialog, dialog[:300]
    assert "What should it come to?" not in dialog and "Hand it over as" not in dialog
    assert await page.evaluate("!document.querySelector('#follow-up-form [name=shape]')")
    await page.evaluate("document.querySelector('#modal')?.close()")
    assert page.console == [], page.console


async def test_messaging_says_when_the_slack_app_needs_files_and_buttons(
    page: Page, office: dict[str, Any]
) -> None:
    """SL1: an app made before files:write is told how to update, step by step."""
    await page.evaluate("Conn.tab = 'chat'; location.hash = 'connections'")
    await page.settle(3.0)
    line = str(await page.evaluate("document.querySelector('[data-slack-files]')?.innerText || ''"))
    assert "Needs a Slack app update" in line, line
    reads = str(
        await page.evaluate("document.querySelector('[data-slack-reads]')?.innerText || ''")
    )
    assert "Reads pictures and files you share" in reads and "Needs a Slack app update" in reads
    await page.evaluate("document.querySelector('[data-action^=\"slack-update:\"]')?.click()")
    await page.settle(0.8)
    steps = str(await page.evaluate("document.querySelector('#modal-content')?.innerText || ''"))
    assert "files:write" in steps and "files:read" in steps and "Interactivity" in steps
    assert "Check again" in steps
    await page.evaluate("document.querySelector('#modal')?.close()")
    assert page.console == [], page.console


async def test_a_schedule_can_post_its_result_to_a_channel(
    page: Page, office: dict[str, Any]
) -> None:
    """SL4: choosing a channel; saving is caught, so nothing is changed or started."""
    await page.evaluate("Settings.tab = 'office'; location.hash = 'settings'")
    await page.settle(3.0)
    row = str(await page.evaluate("document.querySelector('[data-schedule]')?.innerText || ''"))
    assert "Post to Slack" in row, row
    await page.evaluate("document.querySelector('[data-action^=\"schedule-post:\"]')?.click()")
    await page.settle(0.8)
    choices = await page.evaluate(
        "[...document.querySelectorAll('#schedule-post-form [name=post_to] option')]"
        ".map(o => o.textContent)"
    )
    assert choices == ["Nowhere, keep it in the office", "#sales"], choices
    await page.evaluate("document.querySelector('#modal')?.close()")
    await page.evaluate("Office.actions['schedule-new'](TaskRoom.run?.id || 'x')")
    await page.settle(0.6)
    form = str(await page.evaluate("document.querySelector('#modal-content')?.innerText || ''"))
    assert "Post the result to" in form and "for everyone in that channel" in form
    await page.evaluate("document.querySelector('#modal')?.close()")
    assert page.console == [], page.console


async def test_a_dashboard_shows_its_chart_and_opens_as_a_page(
    page: Page, office: dict[str, Any]
) -> None:
    """SL5: drawn from its table; the page link is shown, not visited."""
    await page.evaluate(f"location.hash = 'task/{office['dashboard']}'")
    await page.settle(3.0)
    # Shown where the answer is read: its card, or the editor beside the task.
    drawn = await page.evaluate(
        "!!document.querySelector('.card.answer svg.chart, #answer-editor svg.chart')"
    )
    assert drawn, "the dashboard's table was not drawn"
    if not await page.evaluate("!!document.querySelector('[data-action^=\"answer-page:\"]')"):
        await page.evaluate("AnswerEditor.open = false; rerender()")
        await page.settle(2.0)
    await page.evaluate("document.querySelector('[data-action^=\"answer-page:\"]')?.click()")
    await page.settle(1.0)
    link = str(await page.evaluate("document.getElementById('answer-page-link')?.value || ''"))
    assert "/pages/" in link, link
    await page.evaluate("document.querySelector('#modal')?.close()")
    assert page.console == [], page.console


async def test_performance_shows_how_the_office_works(page: Page, office: dict[str, Any]) -> None:
    """WP1: the cards are drawn; the benchmark (paid) is not pressed."""
    await page.evaluate("Settings.tab = 'performance'; location.hash = 'settings'")
    await page.settle(3.0)
    shown = str(await page.evaluate("document.querySelector('[data-work]')?.innerText || ''"))
    for title in ("How long each step takes", "Reading pages", "Why tasks failed", "Benchmark"):
        assert title in shown, (title, shown[:400])
    assert await page.evaluate("!!document.querySelector('[data-action=\"benchmark-run\"]')")
    assert page.console == [], page.console


async def test_settings_show_the_coding_agent(page: Page, office: dict[str, Any]) -> None:
    await page.evaluate("Settings.tab = 'models'; location.hash = 'settings'")
    await page.settle(3.0)
    card = str(await page.evaluate("document.querySelector('#coding-agent-card')?.innerText || ''"))
    assert "Codex" in card and "Claude Code" in card, card[:300]
    assert page.console == [], page.console


async def test_building_now_shows_each_step_and_a_stop(page: Page, office: dict[str, Any]) -> None:
    """BP1: drawn from the task's events while it runs; nothing here starts a build."""
    html = str(
        await page.evaluate(
            """(() => {
              const at = new Date().toISOString();
              const step = (kind, text, ok) =>
                ({kind: 'build_step', created_at: at, payload: {kind, text, ok}});
              const events = [
                step('phase', 'Codex is writing it, in a sandbox.'),
                step('file', 'Created index.html'),
                step('command', 'Ran node --check app.js', false),
                step('note', 'Fixing the syntax error.'),
              ];
              return buildingNow({status: 'RUNNING'}, events) + '||'
                + buildingNow({status: 'COMPLETED'}, events);
            })()"""
        )
    )
    running, finished = html.split("||")
    assert "Building now" in running and "Created index.html" in running
    assert "Ran node --check app.js" in running and "failed" in running
    assert 'data-action="build-stop"' in running
    assert finished == "", "a finished build shows its record in the build card instead"


async def test_the_live_build_card_shows_the_middle_and_takes_a_comment(
    page: Page, office: dict[str, Any]
) -> None:
    """LB1-LB3, drawn from a live view given here: nothing builds or is sent."""
    shown = json.loads(
        await page.evaluate(
            """(() => {
              const holder = document.createElement('div');
              holder.id = 'live-test';
              document.body.appendChild(holder);
              const at = new Date().toISOString();
              const events = [{kind: 'build_step', created_at: at,
                payload: {kind: 'phase', text: 'Codex is writing it, in a sandbox.'}}];
              holder.innerHTML = buildingNow({status: 'RUNNING', id: 'run-live'}, events);
              LiveBuild.run = 'run-live';
              LiveBuild.draw({
                building: true, began: at, agent: 'Codex', paused: false, earlier: 0,
                plan: [{text: 'Write the page', done: true}, {text: 'Add printing', done: false}],
                now: 'Running npm test',
                steps: [{kind: 'file', text: 'Edited app.js',
                         diff: '@@ -1 +1 @@\\n-old\\n+new', at},
                        {kind: 'owner', text: 'Make it blue', at},
                        {kind: 'thinking', text: 'The grid needs gaps.', at}],
                notes: [{kind: 'build_note', text: 'Make it blue', status: 'QUEUED'}],
                preview: null, changed: 0,
              });
              LiveBuild.pick({tag: 'h1', text: '<b>Hi</b>', selector: 'body > h1'});
              const card = holder.querySelector('[data-live-build]');
              const out = {
                plan: card.querySelector('.live-plan')?.innerText,
                now: card.querySelector('[data-live-now]')?.innerText,
                diff: card.querySelector('.build-diff pre .add')?.textContent,
                you: card.querySelector('.build-step.owner')?.innerText,
                notes: card.querySelector('[data-live-notes]')?.innerText,
                pointed: card.querySelector('[data-live-pointed]')?.innerHTML,
                form: !!card.querySelector('#live-note-form [name=mode]'),
                pause: !!card.querySelector('[data-action="live-pause"]'),
                stop: !!card.querySelector('[data-action="build-stop"]'),
              };
              LiveBuild.stop();
              holder.remove();
              return JSON.stringify(out);
            })()"""
        )
    )
    assert "Write the page" in shown["plan"] and "Add printing" in shown["plan"]
    assert shown["now"] == "Now: Running npm test"
    assert shown["diff"] == "+new"
    assert shown["you"].startswith("✉") and "You: Make it blue" in shown["you"]
    assert "Waiting for its next step" in shown["notes"]
    assert "&lt;b&gt;Hi&lt;/b&gt;" in shown["pointed"], "the page's text is shown as text"
    assert shown["form"] and shown["pause"] and shown["stop"]
    assert page.console == [], page.console


async def test_an_updated_office_says_so_and_offers_a_reload(
    page: Page, office: dict[str, Any]
) -> None:
    """UP1: the phone kept running yesterday's code after an update."""
    seen = await page.evaluate("Office.build")
    assert seen and len(seen) == 12, "the page did not learn which version it runs"
    await page.evaluate("noteBuild('newer000000')")
    bar = str(await page.evaluate("document.getElementById('update-bar')?.innerText || ''"))
    assert "The office has been updated" in bar
    assert await page.evaluate("Office.stale === true")
