"""End-to-end checks in a real browser.

They drive the office as its owner does: sign in with a one-time code, walk
the screens, open the dialogs, and check what a person would see — including
keyboard use, labelling, and layouts from a phone to a wide screen.
"""

import json
from typing import Any
from uuid import uuid4

import pytest
from driver import Page

pytestmark = pytest.mark.browser

SCREENS = [
    ("home", "Good"),
    ("tasks", "What has been handed off"),
    ("discussions", "Talk first"),
    ("office", "How your office is set up"),
    ("rooms", "Talk with the team, where the work is"),
    ("board", "Where every task stands"),
    ("projects", "Your business lines, and the projects in them"),
    ("agents", "Good work starts with good agents"),
    ("knowledge", "A library your office can work with"),
    ("tools", "Tools from servers you trust"),
    ("skills", "A little expertise goes a long way"),
    ("connections", "Bring your office into your world"),
    ("settings", "Your office. Your rules"),
    ("accounts", "Connect the places you work"),
    ("setup", "set up"),
]


async def show(page: Page, screen: str) -> None:
    await page.evaluate(f"location.hash = '{screen}'")
    await page.settle(1.6)


async def confirm_widening(page: Page) -> None:
    """Press Confirm when a change asked for it (it widens what the office may do)."""
    await page.settle(0.5)
    await page.evaluate(
        "(() => { const d = document.getElementById('widen-confirm');"
        " if (d?.open) d.querySelector('button[value=\"confirm\"]').click(); })()"
    )


async def text(page: Page) -> str:
    return str(await page.evaluate("document.querySelector('#content')?.innerText || ''"))


async def test_the_owner_signs_in_with_the_launchers_code(page: Page) -> None:
    signed_in = await page.evaluate("!document.querySelector('#shell').hidden")
    assert signed_in, "the one-time code did not open the office"
    assert await page.evaluate("document.querySelector('#signed-out').hidden") is True
    # The code is single use and must not stay in the address bar.
    assert "code=" not in str(await page.evaluate("location.href"))


async def test_the_owner_signs_in_with_a_password_they_set(page: Page) -> None:
    """LG1: set in Settings, then the sign-in page offers name and password."""
    await show(page, "access")
    assert "Sign in with a password" in await text(page)
    await page.evaluate(
        "(() => { document.querySelector('#login-set-name').value = 'owner';"
        " document.querySelector('#login-new').value = 'a long browser password';"
        " document.querySelector('#login-form button.primary').click(); })()"
    )
    await page.settle(1.5)
    assert "Change password" in await text(page), "the password was not set"
    await page.evaluate("document.querySelector('[data-action=sign-out]').click()")
    await page.settle(1.5)
    assert await page.evaluate("!document.querySelector('#password-form').hidden"), (
        "the sign-in page did not offer the password"
    )
    await page.evaluate(
        "(() => { document.querySelector('#login-name').value = 'Owner';"
        " document.querySelector('#login-password').value = 'a long browser password';"
        " document.querySelector('#password-form button').click(); })()"
    )
    await page.settle(2.0)
    assert await page.evaluate("!document.querySelector('#shell').hidden"), (
        "the password did not open the office"
    )
    # Left as found, so the other checks start from an office without one.
    await show(page, "access")
    await page.evaluate(
        "(() => { document.querySelector('.login-remove').open = true;"
        " document.querySelector('#login-remove-current').value = 'a long browser password';"
        " document.querySelector('#login-remove-form button').click(); })()"
    )
    await page.settle(1.5)
    assert "Turn on" in await text(page)
    assert page.console == [], page.console


async def test_the_wall_takes_widgets_off_and_puts_them_back(page: Page) -> None:
    """W1: arrange, take This month off keeping it, add it back from the shelf."""
    await show(page, "home")
    tiles = (
        "[...document.querySelectorAll('#content [data-widget]')]"
        ".map(t => t.dataset.widget).join(',')"
    )
    assert "month" in str(await page.evaluate(tiles))
    await page.evaluate("document.querySelector('[data-action=widget-arrange]').click()")
    await page.settle(1.2)
    assert await page.evaluate("document.querySelectorAll('.widget-controls').length") >= 4
    await page.evaluate("document.querySelector('[data-action=\"widget-read:month\"]').click()")
    await page.settle(1.2)
    assert "Agents read this" in str(
        await page.evaluate(
            "document.querySelector('[data-widget=month] .widget-controls').innerText"
        )
    )
    await page.evaluate("document.querySelector('[data-action=\"widget-off:month\"]').click()")
    await page.settle(0.8)
    assert "off your wall" in str(await page.evaluate("document.querySelector('#modal').innerText"))
    await page.evaluate("document.querySelector('[data-action=\"widget-keep:month\"]').click()")
    await page.settle(1.5)
    assert "month" not in str(await page.evaluate(tiles))
    await page.evaluate("document.querySelector('[data-action=widget-shelf]').click()")
    await page.settle(1.2)
    await page.evaluate("document.querySelector('[data-action=\"widget-add:month\"]').click()")
    await page.settle(1.5)
    assert "month" in str(await page.evaluate(tiles))
    await page.evaluate("document.querySelector('[data-action=\"widget-read:month\"]').click()")
    await page.settle(1.0)
    await page.evaluate("document.querySelector('[data-action=widget-arrange]').click()")
    await page.settle(1.0)
    assert page.console == [], page.console


async def test_accounts_lists_what_can_be_connected(page: Page) -> None:
    """AC1: Google set up once, services by consent, logins and keys by tab."""
    await show(page, "accounts")
    body = await text(page)
    for name in ("Google", "Microsoft 365", "Slack", "Notion"):
        assert name in body, f"{name} is missing: {body[:300]}"
    assert await page.evaluate("!!document.querySelector('[data-action=\"cal-app:google\"]')")
    await page.evaluate("document.querySelector('[data-action=\"account-tab:logins\"]').click()")
    await page.settle(1.2)
    body = await text(page)
    assert "keeps no logins" in body and "Slack" not in body
    await page.evaluate("document.querySelector('[data-action=\"account-tab:keys\"]').click()")
    await page.settle(1.2)
    assert "API keys" in await text(page)
    await page.evaluate("document.querySelector('[data-action=\"account-tab:all\"]').click()")
    await page.settle(1.0)
    wide = await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert wide, "the accounts list scrolls sideways"
    assert page.console == [], page.console


@pytest.mark.parametrize(("screen", "expected"), SCREENS)
async def test_every_screen_renders_from_live_data(page: Page, screen: str, expected: str) -> None:
    await show(page, screen)
    body = await text(page)
    assert expected.lower() in body.lower(), f"{screen} did not render: {body[:200]}"
    assert "could not load" not in body.lower()
    assert page.console == [], f"{screen} reported errors: {page.console}"


async def test_the_model_dialog_works_wherever_it_is_opened(page: Page) -> None:
    """The bug this suite exists for: a dialog that was empty outside its screen."""
    await show(page, "setup")
    await page.evaluate("document.querySelector('[data-action=conn-add]').click()")
    await page.settle(2.5)
    counts: dict[str, Any] = await page.evaluate(
        "JSON.stringify({providers: document.querySelectorAll('#f-provider option').length,"
        " models: document.querySelectorAll('#model-list .model-row').length,"
        " priced: (document.querySelector('#model-list .model-row small')||{}).textContent||''})"
    )
    import json as _json

    counts = _json.loads(counts)
    assert counts["providers"] >= 10, "the provider list was empty"
    assert counts["models"] >= 1, "no models were listed without a key"
    assert "per million tokens" in counts["priced"] or "no per-token cost" in counts["priced"]
    await page.press("Escape", "Escape", 27)
    assert await page.evaluate("!document.querySelector('#modal').open")


async def test_the_board_offers_only_real_moves(page: Page) -> None:
    await show(page, "board")
    rules = await page.evaluate(
        "[allowedDrop('RUNNING','pause'), allowedDrop('COMPLETED','pause'),"
        " allowedDrop('PAUSED','resume'), allowedDrop('COMPLETED','cancel')].join(',')"
    )
    assert rules == "true,false,true,false"
    # Every card's status box offers transitions, never a state to pretend.
    options = await page.evaluate(
        "[...document.querySelectorAll('.status-pick')].flatMap(s => [...s.options]"
        ".map(o => o.value)).filter(Boolean).join(',')"
    )
    for option in str(options).split(","):
        assert option in ("", "pause", "resume", "cancel"), option


async def test_the_office_can_be_used_from_the_keyboard(page: Page) -> None:
    await show(page, "home")
    await page.evaluate("document.body.focus()")
    reached = []
    for _ in range(12):
        await page.press("Tab", "Tab", 9)
        reached.append(
            await page.evaluate(
                "(document.activeElement.textContent || document.activeElement.ariaLabel || '')"
                ".trim().slice(0, 40)"
            )
        )
    assert any(item for item in reached), "tab reached nothing"
    focus_visible = await page.evaluate(
        "[...document.styleSheets].some(sheet => { try { return [...sheet.cssRules]"
        ".some(rule => (rule.selectorText||'').includes(':focus-visible')); }"
        " catch { return false; } })"
    )
    assert focus_visible, "no visible focus style is defined"


async def test_every_control_says_what_it_is(page: Page) -> None:
    """Screen readers need a name on every field and button."""
    for screen, _ in SCREENS:
        await show(page, screen)
        unnamed = await page.evaluate(
            "JSON.stringify([...document.querySelectorAll("
            "'#content input, #content select, #content textarea')]"
            ".filter(node => !node.labels?.length && !node.getAttribute('aria-label')"
            " && !node.getAttribute('title')).map(node => node.outerHTML.slice(0, 80)))"
        )
        import json as _json

        assert _json.loads(unnamed) == [], f"{screen} has unlabelled fields: {unnamed}"
        nameless = await page.evaluate(
            "JSON.stringify([...document.querySelectorAll('#content button')].filter(node =>"
            " !node.textContent.trim() && !node.getAttribute('aria-label')).map(node =>"
            " node.outerHTML.slice(0, 80)))"
        )
        assert _json.loads(nameless) == [], f"{screen} has buttons with no name: {nameless}"


@pytest.mark.parametrize(("width", "height"), [(390, 780), (768, 1024), (1440, 900)])
async def test_the_layout_holds_from_a_phone_to_a_wide_screen(
    page: Page, width: int, height: int
) -> None:
    await page.viewport(width, height)
    for screen in ("home", "board", "settings"):
        await show(page, screen)
        overflow = await page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert int(overflow) <= 2, f"{screen} scrolls sideways at {width}px by {overflow}px"
    await page.viewport(1440, 900)


async def test_the_office_holds_at_double_size(page: Page) -> None:
    """200% zoom, as a reader who needs larger text would set it."""
    await page.viewport(720, 900, scale=2.0)
    await show(page, "home")
    body = await text(page)
    assert "office" in body.lower()
    overflow = await page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert int(overflow) <= 2, f"the page scrolls sideways at 200%: {overflow}px"
    await page.viewport(1440, 900)


async def paused_at_a_ceiling(page: Page) -> str | None:
    """A task this office has paused at a budget ceiling, if it has one."""
    found = await page.evaluate(
        "(async () => {"
        " const runs = await (await fetch('/runs?limit=60', {credentials:'same-origin'})).json();"
        " const rows = Array.isArray(runs) ? runs : (runs.items || runs.runs || []);"
        " for (const run of rows.filter((r) => r.status === 'PAUSED').slice(0, 8)) {"
        "  const reply = await fetch(`/runs/${run.id}/question`, {credentials:'same-origin'});"
        "  const asked = await reply.json();"
        "  if (asked && asked.kind === 'budget') return run.id;"
        " } return null; })()"
    )
    return str(found) if found else None


async def test_a_task_paused_at_a_ceiling_says_so_and_offers_the_way_out(page: Page) -> None:
    """The owner's report: the task showed no limit setting and could not continue."""
    run_id = await paused_at_a_ceiling(page)
    if run_id is None:
        pytest.skip("no task is paused at a ceiling in this office")
    await show(page, f"task/{run_id}")
    body = await text(page)
    assert "limit" in body.lower(), f"the task room does not mention the limit: {body[:300]}"
    # The way out is offered, and the limits can be edited from the task itself.
    actions = await page.evaluate(
        "Array.from(document.querySelectorAll('[data-action]'))"
        ".map((node) => node.dataset.action).join(' ')"
    )
    assert "limits-edit" in actions, f"no way to change the limits: {actions}"
    assert "ceiling-finish" in actions, f"no way to finish with the work done: {actions}"
    own = await page.evaluate("!!document.querySelector('form[data-form=\"ceiling-own\"] input')")
    assert own, "no place to give an amount of the owner's own"
    # A plain Resume would be refused and change nothing, so it is not offered.
    assert "resume" not in actions.split(), f"a resume that cannot work is offered: {actions}"
    assert page.console == [], f"the task room reported errors: {page.console}"


async def test_a_ceiling_takes_the_owners_own_amount_and_offers_to_finish(page: Page) -> None:
    """6 Oct: the alert offered only its suggested raise, and stopping kept no answer."""
    await show(page, "home")
    card = await page.evaluate(
        "(() => { Ceiling.runId = 'r1'; return ceilingCard({kind: 'tokens', node: 'critique',"
        " budget: {max_tokens: 400000}, spent: {tokens: 397284}}); })()"
    )
    assert 'data-form="ceiling-own"' in card and 'name="amount"' in card
    assert 'value="600000"' in card, "the suggestion is only filled in"
    assert 'data-action="ceiling-finish:r1"' in card and "Finish with what it has" in card
    assert "no more research" in card
    # Typing an amount under what it used is refused before anything is sent.
    sent = await page.evaluate(
        """(async () => {
          const holder = document.createElement('div');
          holder.innerHTML = ceilingChoices('r1', {kind: 'tokens', budget: {max_tokens: 400000},
            spent: {tokens: 397284}});
          document.body.appendChild(holder);
          const form = holder.querySelector('form');
          form.querySelector('input').value = '390000';
          let called = false;
          const real = window.api;
          window.api = async () => { called = true; return {}; };
          try { await Office.forms['ceiling-own'](form); }
          finally { window.api = real; holder.remove(); }
          return called;
        })()"""
    )
    assert sent is False, "an amount below what was used was sent"
    assert page.console == [], page.console


async def test_the_limits_dialog_opens_with_the_tasks_own_numbers(page: Page) -> None:
    run_id = await paused_at_a_ceiling(page)
    if run_id is None:
        pytest.skip("no task is paused at a ceiling in this office")
    await show(page, f"task/{run_id}")
    await page.evaluate("document.querySelector('[data-action=\"limits-edit\"]').click()")
    await page.settle(1.2)
    value = await page.evaluate("document.querySelector('#f-max_tokens')?.value || ''")
    assert value and int(value) > 0, "the limits dialog did not load this task's own tokens"
    assert page.console == [], f"the limits dialog reported errors: {page.console}"


async def test_a_finished_artifact_opens_as_a_document(page: Page) -> None:
    """The owner's report: a finished task showed a JSON dump, not a document."""
    run_id = await page.evaluate(
        """(async () => {
          const runs = await (await fetch('/runs?limit=60', {credentials:'same-origin'})).json();
          const rows = Array.isArray(runs) ? runs : (runs.items || runs.runs || []);
          for (const run of rows) {
            const list = await fetch(`/runs/${run.id}/artifacts`, {credentials:'same-origin'});
            const artifacts = await list.json();
            if (artifacts.length) return run.id;
          }
          return null;
        })()"""
    )
    if not run_id:
        pytest.skip("this office has no task with artifacts")
    await show(page, f"task/{run_id}")
    await page.evaluate("""document.querySelector('[data-action="tab:artifacts"]').click()""")
    await page.settle(1.2)
    await page.evaluate("""document.querySelector('[data-action^="artifact:"]').click()""")
    await page.settle(1.8)
    shown = str(await page.evaluate("document.querySelector('#modal-content')?.innerText || ''"))
    assert shown.strip(), "the artifact did not open"
    # A document, not a payload.
    assert '"research_question"' not in shown, "raw field names are still on screen"
    assert not shown.lstrip().startswith("{"), "the reader is showing JSON"
    structured = await page.evaluate("!!document.querySelector('#modal-content .document h2')")
    assert structured, f"no headings in the reader: {shown[:200]}"
    formats = await page.evaluate(
        """Array.from(document.querySelectorAll('#modal-content a[href*="download"]'))
             .map((node) => new URL(node.href).searchParams.get('format')).join(' ')"""
    )
    assert "docx" in formats and "xlsx" in formats, f"formats offered: {formats}"
    assert page.console == [], f"the reader reported errors: {page.console}"


async def test_the_handbook_reads_from_the_running_office(page: Page) -> None:
    """Health, performance and the API list must come from the office itself."""
    await show(page, "handbook")
    for tab, expected in (
        ("running", "scripts.launch"),
        ("concepts", "A work area"),
        ("using", "Answer and continue"),
        ("health", "Database"),
        ("performance", "Model calls"),
        ("api", "routes, read from the office"),
    ):
        await page.evaluate(
            f"document.querySelector('[data-action=\"handbook-tab:{tab}\"]').click()"
        )
        await page.settle(1.6)
        body = await text(page)
        assert expected in body, f"{tab} did not render: {body[:200]}"
        assert "could not load" not in body.lower()
    # The live panels show this installation, not a written-down copy of it.
    await page.evaluate("document.querySelector('[data-action=\"handbook-tab:health\"]').click()")
    await page.settle(1.6)
    shown = await text(page)
    version = await page.evaluate(
        """(async () => {
             const reply = await fetch('/status', {credentials: 'same-origin'});
             return (await reply.json()).version;
           })()"""
    )
    assert str(version) in shown, "the handbook does not show this office's own version"
    assert page.console == [], f"the handbook reported errors: {page.console}"


async def test_a_finished_task_shows_its_answer_first(page: Page) -> None:
    """The answer used to be three clicks away, inside an engineering log."""
    run_id = await page.evaluate(
        """(async () => {
             const reply = await fetch('/runs?limit=60', {credentials: 'same-origin'});
             const runs = await reply.json();
             const rows = Array.isArray(runs) ? runs : (runs.items || runs.runs || []);
             for (const run of rows.filter((r) => r.status === 'COMPLETED').slice(0, 8)) {
               const got = await fetch(`/runs/${run.id}/artifacts`, {credentials: 'same-origin'});
               const artifacts = await got.json();
               if (artifacts.some((a) => ['synthesis', 'strategy_proposal'].includes(a.type))) {
                 return run.id;
               }
             }
             return null;
           })()"""
    )
    if not run_id:
        pytest.skip("this office has no finished task with an answer")
    await show(page, f"task/{run_id}")
    await page.settle(2.5)
    assert await page.evaluate("!!document.querySelector('.card.answer')"), "no answer shown"
    body = await text(page)
    # The answer is above the tabs, not inside an artifact three clicks away.
    assert body.index("The answer") < body.index("Activity")
    folded = await page.evaluate(
        """(() => {
             const d = document.querySelector('.card.answer details.working');
             return d ? `${d.open}` : 'none';
           })()"""
    )
    assert folded in ("false", "none"), "the working must start folded"
    assert page.console == [], f"the task room reported errors: {page.console}"


async def test_opening_the_working_survives_the_rooms_own_refresh(page: Page) -> None:
    """It flashed shut: the room redrew every four seconds under the reader."""
    run_id = await page.evaluate(
        """(async () => {
             const reply = await fetch('/runs?limit=60', {credentials: 'same-origin'});
             const runs = await reply.json();
             const rows = Array.isArray(runs) ? runs : (runs.items || runs.runs || []);
             for (const run of rows.filter((r) => r.status === 'COMPLETED').slice(0, 8)) {
               const got = await fetch(`/runs/${run.id}/artifacts`, {credentials: 'same-origin'});
               const artifacts = await got.json();
               if (artifacts.some((a) => ['synthesis', 'strategy_proposal'].includes(a.type))) {
                 return run.id;
               }
             }
             return null;
           })()"""
    )
    if not run_id:
        pytest.skip("this office has no finished task with an answer")
    await show(page, f"task/{run_id}")
    await page.settle(2.0)
    fold = ".card.answer details.working"
    if not await page.evaluate(f"!!document.querySelector('{fold}')"):
        pytest.skip("this task's answer has no working to fold")

    await page.evaluate(f"document.querySelector('{fold} > summary').click()")
    await page.settle(0.4)
    assert await page.evaluate(f"document.querySelector('{fold}').open") is True

    # A finished task asks not to be redrawn at all.
    assert await page.evaluate("Office.screens.task.quiet()") is True

    # And a redraw, forced as a running task would cause, keeps it open.
    await page.evaluate("TaskRoom.run.status = 'RUNNING'")
    await page.evaluate("rerender()")
    await page.settle(1.8)
    assert await page.evaluate(f"document.querySelector('{fold}').open") is True, "it flashed shut"

    # Closing it is remembered too, so it does not spring back open.
    await page.evaluate(f"document.querySelector('{fold} > summary').click()")
    await page.settle(0.4)
    await page.evaluate("rerender()")
    await page.settle(1.5)
    assert await page.evaluate(f"document.querySelector('{fold}').open") is False
    assert page.console == [], f"the task room reported errors: {page.console}"


async def test_discussions_open_under_in_conversation(page: Page) -> None:
    """TF1, UI-1: the threads under way are one click from anywhere, in the sidebar."""
    await page.viewport(1440, 900)
    started = await page.evaluate(
        "api('/topics', {method: 'POST', body: {text: '@Nobody-here a sidebar check'}})"
        ".then((r) => r.topic.id)"
    )
    await show(page, "home")
    await page.settle(1.5)
    listed = await page.evaluate(
        "[...document.querySelectorAll('#nav .side-thread')].map((a) => a.getAttribute('href'))"
    )
    assert f"#topic/{started}" in listed, listed
    await page.evaluate(
        f"document.querySelector('#nav .side-thread[href=\"#topic/{started}\"]').click()"
    )
    await page.settle(2.0)
    assert await page.evaluate("Office.view") == "topic"
    current = await page.evaluate(
        "document.querySelector('#nav .side-thread.active')?.getAttribute('href')"
    )
    assert current == f"#topic/{started}"
    await page.evaluate(
        f"api('/topics/{started}', {{method: 'PUT', body: {{status: 'archived'}}}})"
    )
    assert page.console == [], f"the sidebar reported errors: {page.console}"


async def test_office_settings_holds_what_configures_the_office(page: Page) -> None:
    """TF1, UI-1, UI-2e: the places to work in; configuration one step away, in
    a window over the page, nine sections grouped by the decision being made."""
    await page.viewport(1440, 900)
    await show(page, "home")
    places = await page.evaluate(
        "[...document.querySelectorAll('#nav .side-row .side-text')].map((el) => el.textContent)"
    )
    assert places == ["Desk", "Library", "Calendar", "Spaces", "Settings", "Help"], places
    await page.evaluate("document.querySelector('#nav a.side-row[href=\"#office\"]').click()")
    await page.settle(2.0)
    assert await page.evaluate("settingsWindowOpen()") is True
    assert await page.evaluate("location.hash") == "#home", "the page behind stays"
    sections = await page.evaluate(
        "[...document.querySelectorAll('#settings-nav .sw-section')].map((b) => b.textContent)"
    )
    assert sections == [
        "General",
        "Team",
        "Models & spending",
        "Permissions & rules",
        "Connections",
        "Plugins & tools",
        "Workspaces",
        "Calendar",
        "Access & devices",
        "Health",
    ], sections
    groups = await page.evaluate(
        "[...document.querySelectorAll('#settings-nav .side-label')].map((p) => p.textContent)"
    )
    assert groups == ["Office", "Reach", "System"], groups
    # Every part draws the screen it was, inside the window.
    parts = await page.evaluate("SETTINGS_SECTIONS.map((s) => [s.id, s.parts.length])")
    for section, count in parts:
        for index in range(count):
            await page.evaluate(
                f"(SettingsWindow.section = '{section}', SettingsWindow.part = {index},"
                " settingsWindowDraw())"
            )
            await page.settle(0.8)
            drawn = await page.evaluate("document.querySelector('#settings-body').innerText")
            assert drawn.strip() and "could not load" not in drawn, (section, index, drawn[:200])
    # A change redraws the part in the window, not the page behind it.
    await page.evaluate(
        "document.querySelector('[data-action=\"settings-section:models\"]').click()"
    )
    await page.settle(1.5)
    await page.evaluate("document.querySelector('[data-action=\"settings-part:1\"]').click()")
    await page.settle(1.5)
    await page.evaluate("rerender()")
    await page.settle(1.5)
    chosen = await page.evaluate("document.querySelector('#settings-parts .active')?.textContent")
    assert chosen == "Limits", chosen
    await page.evaluate("document.getElementById('settings-window').close()")
    await page.settle(0.8)
    assert await page.evaluate("settingsWindowOpen()") is False
    # Cmd+comma (Ctrl+comma) opens it from anywhere, at the section last used.
    await page.evaluate(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: ',', metaKey: true}))"
    )
    await page.settle(1.5)
    assert await page.evaluate("settingsWindowOpen()") is True
    title = await page.evaluate("document.getElementById('settings-window-title').textContent")
    assert title == "Models & spending", title
    assert await page.evaluate(
        "document.getElementById('settings-window').contains(document.activeElement)"
    ), "the focus is in the window"
    await page.evaluate("document.getElementById('settings-window').close()")
    await page.settle(0.8)
    # The old page still answers a link to it, and opens the window too.
    await show(page, "office")
    await page.settle(1.0)
    hub = await page.evaluate(
        "[...document.querySelectorAll('.office-hub a')].map((a) => a.getAttribute('href'))"
    )
    for screen in ("#agents", "#settings", "#tools", "#skills", "#policies", "#connections"):
        assert screen in hub, hub
    await page.evaluate("document.querySelector('[data-action=\"settings-open\"]').click()")
    await page.settle(1.5)
    assert await page.evaluate("settingsWindowOpen()") is True
    await page.evaluate("document.getElementById('settings-window').close()")
    assert page.console == [], page.console


async def test_one_sidebar_on_every_screen(page: Page) -> None:
    """UI-1: one sidebar for every screen; the top menu is
    retired, even for a browser that once chose it. A rail when narrow, a row
    on a phone."""
    await page.viewport(1440, 900)
    await page.evaluate("localStorage.setItem('office:navLayout', JSON.stringify('top'))")
    await page.evaluate("location.reload()")
    await page.settle(2.5)
    await show(page, "board")
    await page.settle(1.2)
    side = await page.evaluate(
        """(() => {
          const aside = document.querySelector('.shell > aside').getBoundingClientRect();
          const main = document.querySelector('main').getBoundingClientRect();
          return {side: document.body.classList.contains('nav-side'),
                  width: Math.round(aside.width), height: Math.round(aside.height),
                  mainLeft: Math.round(main.left)};
        })()"""
    )
    assert side["side"] is True, side
    assert 200 <= side["width"] <= 280 and side["height"] >= 880, side
    assert side["mainLeft"] >= side["width"] - 2, "the page sits beside the menu"
    offered = "document.querySelector('[data-action^=\"nav-layout\"]:not([hidden])')"
    assert await page.evaluate(offered) is None, "no choice of a top menu is offered"
    fit = await page.evaluate(f"{PILL_FIT}('#nav', 'a.active')")
    assert fit and max(fit.values()) < 2, f"the pill sits on the place chosen: {fit}"
    await page.viewport(820, 900)
    await page.settle(1.0)
    rail = await page.evaluate(
        """(() => {
          const aside = document.querySelector('.shell > aside').getBoundingClientRect();
          const brand = document.querySelector('.shell > aside .brand').getBoundingClientRect();
          return {width: Math.round(aside.width), left: brand.left, right: brand.right,
                  edge: aside.right};
        })()"""
    )
    assert rail["width"] < 100 and rail["left"] >= 0 and rail["right"] <= rail["edge"], rail
    await page.viewport(390, 780)
    await page.settle(1.0)
    overflow = await page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert int(overflow) <= 2, f"the phone layout scrolls sideways by {overflow}px"
    await page.viewport(1440, 900)
    await page.evaluate("localStorage.removeItem('office:navLayout')")
    assert page.console == [], page.console


async def test_the_task_list_is_not_shown_where_the_sidebar_has_no_room(page: Page) -> None:
    """At 880px the sidebar is icons, and on a phone it is a row of them."""
    await page.viewport(390, 780)
    await page.settle(1.2)
    hidden = await page.evaluate(
        """(() => [...document.querySelectorAll('.nav-section, .nav-tasks')]
             .every((list) => getComputedStyle(list).display === 'none'))()"""
    )
    assert hidden, "a nested list cannot fit a row of icons"
    overflow = await page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert int(overflow) <= 2, f"the phone layout scrolls sideways by {overflow}px"
    await page.viewport(1440, 900)
    await page.settle(0.8)


async def test_a_new_task_says_what_it_should_come_to(page: Page) -> None:
    """The shape is chosen before the work starts, not at the download."""
    await show(page, "tasks")
    await page.evaluate("document.querySelector('[data-action=\"new-task\"]').click()")
    await page.settle(1.4)
    shapes = await page.evaluate(
        "Array.from(document.querySelectorAll('#f-shape option')).map(o => o.value)"
    )
    assert "comparison" in shapes and "slides" in shapes, f"shapes offered: {shapes}"

    # A comparison suggests a spreadsheet, until the owner says otherwise.
    await page.evaluate(
        """(() => {
          const shape = document.querySelector('#f-shape');
          shape.value = 'comparison';
          shape.dispatchEvent(new Event('change'));
        })()"""
    )
    await page.settle(0.4)
    assert await page.evaluate("document.querySelector('#f-format').value") == "xlsx"
    assert page.console == [], f"the new-task dialog reported errors: {page.console}"


async def test_setup_offers_a_sample_and_never_calls_it_a_live_test(page: Page) -> None:
    """A new owner can see finished work before choosing a provider."""
    await show(page, "setup")
    body = await text(page)
    assert "finished task first" in body.lower(), body[:300]
    # Whatever it says, it must not read as proof that a model works.
    offered = await page.evaluate(
        """(() => {
          const rows = [...document.querySelectorAll('.setup-step')];
          const row = rows.find((r) => r.innerText.toLowerCase().includes('finished task first'));
          return row ? row.innerText : '';
        })()"""
    )
    said = str(offered).lower()
    assert "nothing is called and nothing is spent" in said
    assert "not real work" in said
    assert page.console == [], f"the setup screen reported errors: {page.console}"


async def test_a_task_is_headed_by_a_short_title_with_the_ask_beneath(page: Page) -> None:
    """The owner's report: the whole objective was set in bold as the title."""
    run_id = await page.evaluate(
        """(async () => {
          // The overview already carries every task's objective and title, so
          // this needs one request rather than one per task.
          const view = await (await fetch('/office/overview', {credentials:'same-origin'})).json();
          const rows = view.tasks || view.recent || [];
          const long = rows.find((t) => (t.objective || '').length > 90);
          return long ? long.run_id : null;
        })()"""
    )
    if not run_id:
        pytest.skip("no task in this office has an objective long enough to shorten")
    await show(page, f"task/{run_id}")
    heading = await page.evaluate("document.querySelector('#content h1')?.innerText || ''")
    assert 0 < len(str(heading)) <= 74, f"the title is not short: {heading!r}"
    asked = await page.evaluate("document.querySelector('.card.asked p')?.innerText || ''")
    assert len(str(asked)) > len(str(heading)), "the full ask is not shown beneath the title"
    assert page.console == [], f"the task room reported errors: {page.console}"


async def finished_report(page: Page, statuses: tuple[str, ...] = ("COMPLETED",)) -> str | None:
    """A finished task that answered in writing; a build's next step is a version."""
    wanted = json.dumps(list(statuses))
    found = await page.evaluate(
        f"""(async () => {{
          const statuses = {wanted};
          const view = await api('/office/overview');
          for (const task of view.tasks || []) {{
            if (!statuses.includes(task.status)) continue;
            const run = await api(`/runs/${{task.run_id}}`);
            if (run.state?.build_folder || run.state?.plan?.task_kind === 'build') continue;
            return task.run_id;
          }}
          return null;
        }})()"""
    )
    return str(found) if found else None


async def test_a_finished_task_offers_the_next_question(page: Page) -> None:
    run_id = await finished_report(page)
    if not run_id:
        pytest.skip("no finished task in this office")
    await show(page, f"task/{run_id}")
    assert await page.evaluate("!!document.querySelector('[data-action=\"follow-up\"]')"), (
        "a finished task offers no way to continue"
    )
    await page.evaluate("document.querySelector('[data-action=\"follow-up\"]').click()")
    await page.settle(1.2)
    said = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "costs money" in str(said).lower(), "the follow-up does not say it is paid work"
    assert await page.evaluate("!!document.querySelector('#f-objective')")
    await page.press("Escape", "Escape", 27)


async def test_approve_says_why_it_cannot_be_pressed(page: Page) -> None:
    """The owner's report: clicking Approve did nothing and said nothing.

    The confirmation was enforced only by a toast, so an unticked box left a
    live-looking button that sent no request.
    """
    run_id = await page.evaluate(
        """(async () => {
             const pending = await (await fetch('/approvals/pending',
               {credentials: 'same-origin'})).json();
             return pending.length ? pending[0].approval.run_id : null;
           })()"""
    )
    if not run_id:
        pytest.skip("nothing is waiting for approval in this office")
    await page.evaluate(f"location.hash = 'task/{run_id}'")
    await page.settle(2.2)
    await page.evaluate("document.querySelector('[data-action=approval]').click()")
    await page.settle(2.0)

    state = await page.evaluate(
        "JSON.stringify({disabled: document.querySelector('#approve-button')?.disabled,"
        " why: document.querySelector('#approve-button')?.title || ''})"
    )
    import json as _json

    before = _json.loads(state)
    assert before["disabled"] is True, "Approve is pressable before the box is ticked"
    assert "tick the box" in before["why"].lower(), "it does not say why it cannot be pressed"

    # Ticking it arms the button, without sending anything by itself.
    await page.evaluate("document.querySelector('#approve-check').click()")
    await page.settle(0.4)
    assert await page.evaluate("document.querySelector('#approve-button').disabled") is False
    assert page.console == [], f"the approval dialog reported errors: {page.console}"
    await page.press("Escape", "Escape", 27)


async def test_a_finished_answer_offers_every_format_it_can_be_taken_as(page: Page) -> None:
    """The owner's report: a task asked for slides and the card offered none.

    PowerPoint reached the artifact viewer and never reached the answer card,
    which is the only place most people look.
    """
    run_id = await finished_report(page)
    if not run_id:
        pytest.skip("no finished task in this office")
    await page.evaluate(f"location.hash = 'task/{run_id}'")
    await page.settle(2.4)
    if not await page.evaluate("!!document.querySelector('.card.answer')"):
        pytest.skip("this finished task recorded no answer")
    offered = await page.evaluate(
        "JSON.stringify([...document.querySelectorAll('.card.answer .button-row a')]"
        ".map(a => a.getAttribute('href')))"
    )
    assert "format=pptx" in str(offered), f"slides are not offered: {offered}"
    assert "format=docx" in str(offered) and "format=xlsx" in str(offered)
    assert page.console == [], f"the task room reported errors: {page.console}"


async def test_a_room_answers_without_a_reload(page: Page) -> None:
    """W3: a message goes in, the answer comes back into the open room.

    It names an agent no office has, so the office answers for itself: no model
    is called and nothing is spent. It makes its own project and removes it, so
    it leaves nothing behind in a real room.
    """
    name = f"Browser room {await page.evaluate('Date.now()')}"
    project_id = await page.evaluate(
        f"api('/projects', {{method: 'POST', body: {{name: '{name}'}}}}).then((p) => p.id)"
    )
    try:
        await page.evaluate(f"location.hash = 'room/{project_id}'")
        await page.settle(1.8)
        assert "Nobody has said anything here yet" in await text(page)
        await page.evaluate(
            "(() => { const t = document.querySelector('#room-text');"
            " t.value = '@Nobody-here are you there?'; t.focus();"
            " document.querySelector('#room-form').requestSubmit(); })()"
        )
        await page.settle(4.0)
        feed = await page.evaluate("document.querySelector('#room-feed').innerText")
        assert "@Nobody-here are you there?" in feed
        assert "There is no agent called @Nobody-here" in feed
        # What was typed is gone from the box, and the page never reloaded.
        assert await page.evaluate("document.querySelector('#room-text').value") == ""
        assert page.console == [], page.console
    finally:
        await page.evaluate(
            f"api('/projects/{project_id}', {{method: 'DELETE', body: {{confirm: '{name}'}}}})"
        )


async def test_a_rooms_documents_tab_opens_and_says_when_there_are_none(page: Page) -> None:
    """W6: the tab switches without a reload; a new project has nothing yet."""
    name = f"Browser docs {await page.evaluate('Date.now()')}"
    project_id = await page.evaluate(
        f"api('/projects', {{method: 'POST', body: {{name: '{name}'}}}}).then((p) => p.id)"
    )
    try:
        await page.evaluate(f"location.hash = 'room/{project_id}'")
        await page.settle(1.8)
        await page.evaluate(
            "document.querySelector('[data-action=\"room-tab:documents\"]').click()"
        )
        await page.settle(1.2)
        assert await page.evaluate("document.querySelector('#room-messages').hidden") is True
        assert "No documents yet" in await page.evaluate(
            "document.querySelector('#room-documents').innerText"
        )
        selected = await page.evaluate(
            "document.querySelector('#room-tab-documents').getAttribute('aria-selected')"
        )
        assert selected == "true"
        assert page.console == [], page.console
    finally:
        await page.evaluate(
            f"api('/projects/{project_id}', {{method: 'DELETE', body: {{confirm: '{name}'}}}})"
        )


async def test_a_new_room_is_made_from_the_rooms_page_and_opens(page: Page) -> None:
    """A room without starting a task first; it opens, and the sidebar lists it."""
    name = f"Browser new room {await page.evaluate('Date.now()')}"
    await show(page, "rooms")
    await page.evaluate("document.querySelector('[data-action=\"room-new\"]').click()")
    await page.settle(0.6)
    await page.evaluate(
        f"(() => {{ document.querySelector('#f-name').value = '{name}';"
        " document.querySelector('#room-new-form').requestSubmit(); })()"
    )
    await page.settle(2.0)
    project_id = await page.evaluate("Office.param")
    try:
        assert await page.evaluate("Office.view") == "room"
        assert name in await text(page)
        assert "Nobody has said anything here yet" in await text(page)
        # A project's room is its first discussion.
        assert await page.evaluate("RoomState.topic?.home") is True
        assert page.console == [], page.console
    finally:
        await page.evaluate(
            f"api('/projects/{project_id}', {{method: 'DELETE', body: {{confirm: '{name}'}}}})"
        )


async def test_a_task_opens_its_projects_room(page: Page) -> None:
    """From the work to the conversation about it, in one click."""
    tasks = await page.evaluate("api('/runs?limit=1')")
    if not tasks:
        pytest.skip("this office has no tasks")
    run = tasks[0]
    await page.evaluate(f"location.hash = 'task/{run['id']}'")
    await page.settle(2.0)
    link = f"a[href='#room/{run['project_id']}']"
    assert await page.evaluate(f'!!document.querySelector("{link}")'), "no way into the room"
    await page.evaluate(f'document.querySelector("{link}").click()')
    await page.settle(2.0)
    assert await page.evaluate("Office.view") == "room"
    assert await page.evaluate("Office.param") == run["project_id"]
    assert page.console == [], page.console


async def test_the_tools_catalog_opens_and_fills_in_the_form(page: Page) -> None:
    """N10: pick a server from the catalog; the form arrives filled in."""
    await show(page, "tools")
    await page.evaluate("document.querySelector('[data-action=\"mcp-catalog\"]').click()")
    await page.settle(1.5)
    shown = await page.evaluate("document.querySelector('#modal-content').innerText")
    assert "Research and search" in shown and "Exa" in shown
    assert "Sign in with your account" in shown, "account servers must say how they sign in"
    assert "does not let the office register itself" in shown, "Asana and HubSpot say why not"
    add = '[data-action="mcp-catalog-add:microsoft-learn"]'
    if not await page.evaluate(f"!!document.querySelector('{add}')"):
        pytest.skip("Microsoft Learn is already added in this office")
    await page.evaluate(f"document.querySelector('{add}').click()")
    await page.settle(0.8)
    assert await page.evaluate("document.querySelector('#f-url').value") == (
        "https://learn.microsoft.com/api/mcp"
    )
    assert await page.evaluate("document.querySelector('#f-name').value") == "Microsoft Learn"
    # Filled in, not submitted: nothing is added until the owner presses the button.
    await page.evaluate("document.querySelector('[data-action=\"close\"]').click()")
    assert page.console == [], page.console


async def test_a_reviewed_tool_can_be_given_a_price_and_says_so(page: Page) -> None:
    """N12: the review form saves a price per call; the tool says it is paid."""
    await show(page, "tools")
    servers = await page.evaluate("Tools.servers.filter(s => s.tools.length).map(s => s.id)")
    if not servers:
        pytest.skip("no server in this office offers a tool to review")
    server = servers[0]
    before = await page.evaluate(f"Tools.servers.find(s => s.id === '{server}').tools[0]")
    await page.evaluate(f"Office.actions['mcp-review']('{server}')")
    await page.settle(1.0)
    set_price = (
        "(() => { document.querySelector('#f-stance-0').value = 'allowed';"
        " document.querySelector('#f-price-0').value = '0.15'; })()"
    )
    await page.evaluate(set_price)
    await page.evaluate("document.querySelector('[data-action=\"mcp-decide:0\"]').click()")
    await confirm_widening(page)
    await page.settle(1.5)
    shown = await page.evaluate("document.querySelector('#modal-content').innerText")
    assert "Paid · up to $0.15" in shown, shown[:400]
    assert await page.evaluate("document.querySelector('#f-price-0').value") == "0.15"

    # Put it back as it was.
    stance = before.get("chosen") or "refused"
    price = before.get("price_usd") or ""
    restore = (
        f"(() => {{ document.querySelector('#f-stance-0').value = '{stance}';"
        f" document.querySelector('#f-price-0').value = '{price}'; }})()"
    )
    await page.evaluate(restore)
    await page.evaluate("document.querySelector('[data-action=\"mcp-decide:0\"]').click()")
    await page.settle(1.2)
    await page.evaluate("document.querySelector('[data-action=\"close\"]')?.click()")
    assert page.console == [], page.console


async def test_a_new_task_is_titled_from_its_ask_unless_titled_by_hand(page: Page) -> None:
    """The title follows what is typed below it, until the owner writes their own."""
    await page.evaluate("Office.actions['new-task']()")
    await page.settle(1.2)
    type_ask = (
        "(() => { const o = document.querySelector('#f-objective');"
        " o.value = 'Please research what the Wwft requires of an EMI, so that we can plan.';"
        " o.dispatchEvent(new Event('input')); })()"
    )
    await page.evaluate(type_ask)
    await page.settle(1.2)
    suggested = await page.evaluate("document.querySelector('#f-title').value")
    assert suggested and "Wwft" in suggested and "Please" not in suggested, suggested
    # A title typed by hand is kept when the ask changes.
    await page.evaluate(
        "(() => { const t = document.querySelector('#f-title'); t.value = 'My own title';"
        " t.dispatchEvent(new Event('input')); })()"
    )
    await page.evaluate(type_ask.replace("an EMI", "a payment institution"))
    await page.settle(1.2)
    assert await page.evaluate("document.querySelector('#f-title').value") == "My own title"
    await page.evaluate("document.querySelector('[data-action=\"close\"]').click()")
    assert page.console == [], page.console


async def test_a_task_waiting_for_answers_asks_in_words_not_codes(page: Page) -> None:
    """E1: the Chief's questions, with a way to answer; no raw stop codes."""
    card = await page.evaluate(
        """attentionFor({status: "NEEDS_HUMAN_REVIEW", error: "needs_owner_answers",
            state: {stopped_reason: "needs_owner_answers",
                    plan: {owner_questions: ["Which six industries do you mean?"]}}})"""
    )
    assert "Which six industries do you mean?" in card
    assert 'data-action="answer-owner"' in card
    assert "needs_owner_answers" not in card.replace('data-action="answer-owner"', "")
    older = await page.evaluate(
        """attentionFor({status: "NEEDS_HUMAN_REVIEW", error: "evidence_not_reachable",
            state: {stopped_reason: "evidence_not_reachable"}})"""
    )
    assert "evidence_not_reachable" not in older
    assert "researches what it can reach" in older
    assert page.console == [], page.console


async def test_a_new_task_opened_on_a_task_starts_from_it(page: Page) -> None:
    """X1: its project is chosen, and it offers to build on the task on screen."""
    tasks = await page.evaluate("api('/office/overview').then(o => o.tasks.map(t => t.run_id))")
    if not tasks:
        pytest.skip("this office has no task to start from")
    await show(page, f"task/{tasks[-1]}")
    await page.settle(1.0)
    project = await page.evaluate("TaskRoom.run.project_id")
    await page.evaluate("Office.actions['new-task']()")
    await page.settle(1.2)
    assert await page.evaluate("document.querySelector('#f-project')?.value") == project
    assert await page.evaluate("document.querySelector('#f-build-on')?.checked") is True
    await page.evaluate("document.querySelector('[data-action=\"close\"]')?.click()")
    assert page.console == [], page.console


async def test_a_finished_task_offers_rework_and_sends_the_note(page: Page) -> None:
    """C4: Rework sits beside Ask a follow-up; the dialog sends the note and the part."""
    finished = await finished_report(page, ("COMPLETED", "FAILED", "CANCELLED"))
    if not finished:
        pytest.skip("this office has no finished task")
    await show(page, f"task/{finished}")
    await page.settle(1.0)
    assert await page.evaluate("!!document.querySelector('[data-action=\"rework\"]')")
    # Capture the request rather than start real work.
    await page.evaluate(
        "(() => { const real = api; window.__sent = null; window.api = (p, o) =>"
        " p.endsWith('/rework')"
        " ? (window.__sent = {p, body: o.body}, Promise.resolve(TaskRoom.run))"
        " : real(p, o); })()"
    )
    await page.evaluate("document.querySelector('[data-action=\"rework\"]').click()")
    await page.settle(0.8)
    await page.evaluate(
        "document.querySelector('#f-rework-comment').value = 'Add the EU vendors';"
        " document.querySelector('#f-rework-target').value = 'research';"
    )
    await page.evaluate("document.querySelector('[data-action=\"rework-send\"]').click()")
    await page.settle(1.0)
    sent = await page.evaluate("window.__sent")
    assert sent["p"].endswith(f"/runs/{finished}/rework")
    assert sent["body"] == {"comment": "Add the EU vendors", "revision_target": "research"}
    # No note is fine; the office decides what to improve.
    await page.evaluate("window.__sent = null")
    await page.evaluate("document.querySelector('[data-action=\"rework\"]').click()")
    await page.settle(0.8)
    await page.evaluate("document.querySelector('[data-action=\"rework-send\"]').click()")
    await page.settle(1.0)
    again = await page.evaluate("window.__sent")
    assert again and again["body"]["comment"] == "", "an empty note was refused"
    assert page.console == [], page.console


# Readability and focus.

LONG_TITLE = (
    "对上述六个垂直行业按痛点强度、付费意愿、获客难度、竞争程度和个人创业可行性"
    "进行调研并给出前三名及首个MVP方案，再补充每个行业的首批客户画像与验证路径"
)

MEASURE_ANSWER = """(() => {
  const card = document.createElement('div');
  card.className = 'card answer';
  const words = 'word '.repeat(4000);
  card.innerHTML = '<div class="artifact-body document"><p>' + words + '</p></div>';
  document.querySelector('#content').appendChild(card);
  const body = card.querySelector('.artifact-body');
  const text = body.querySelector('p');
  const measured = {
    width: body.getBoundingClientRect().width,
    size: parseFloat(getComputedStyle(text).fontSize),
    scrolls: body.scrollHeight > body.clientHeight + 1,
  };
  card.remove();
  return measured;
})()"""

TINY_TEXT = """(() => {
  const speaks = (el) => [...el.childNodes].some(
    (n) => n.nodeType === 3 && n.textContent.trim());
  const shown = [...document.querySelectorAll('#content *, header *, aside *')]
    .filter((el) => speaks(el) && el.offsetParent !== null && !el.closest('.count'));
  return shown
    .map((el) => [el.tagName + '.' + el.className,
                  parseFloat(getComputedStyle(el).fontSize),
                  el.textContent.trim().slice(0, 30)])
    .filter(([, size]) => size < 12);
})()"""

TITLE_LINES = """((title) => {
  const holder = document.createElement('div');
  holder.innerHTML = heading('TASK', title, 'x');
  document.querySelector('#content').prepend(holder);
  const h1 = holder.querySelector('h1');
  const high = h1.getBoundingClientRect().height;
  const line = parseFloat(getComputedStyle(h1).lineHeight);
  const result = {lines: Math.round(high / line), title: h1.title};
  holder.remove();
  return result;
})"""


async def test_prose_keeps_a_reading_measure_on_a_wide_screen(page: Page) -> None:
    """R1: an answer is read at a comfortable line length, not the screen's width."""
    await page.viewport(1440, 900)
    await show(page, "home")
    measured = await page.evaluate(MEASURE_ANSWER)
    assert measured["width"] <= 42 * 16 + 1, measured
    assert measured["size"] >= 16, measured
    assert measured["scrolls"] is False, "the answer never hides behind its own scroll bar"


@pytest.mark.parametrize(
    "screen",
    ["home", "tasks", "board", "projects", "agents", "knowledge", "tools", "settings", "handbook"],
)
async def test_no_text_is_smaller_than_twelve_pixels(page: Page, screen: str) -> None:
    await page.viewport(1440, 900)
    await show(page, screen)
    tiny = await page.evaluate(TINY_TEXT)
    assert tiny == [], tiny[:10]


async def test_a_long_title_stays_within_three_lines(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    lines = await page.evaluate(f"{TITLE_LINES}({LONG_TITLE!r})")
    assert lines["lines"] <= 3, lines
    assert lines["title"].startswith("对上述六个"), "the full title is on hover"


async def _any_task(page: Page) -> str:
    tasks = await page.evaluate("api('/office/overview').then(o => o.tasks.map(t => t.run_id))")
    if not tasks:
        pytest.skip("this office has no task to show")
    return str(tasks[0])


PLACES = """(() => {
  const box = (sel) => document.querySelector(sel)?.getBoundingClientRect();
  const main = box('.task-main'), aside = box('.task-columns > .task-aside');
  const tops = [...document.querySelectorAll('.task-actions button, .task-actions a')]
    .map((el) => el.getBoundingClientRect().top);
  const row = tops.length && Math.max(...tops) - Math.min(...tops) < 6 ? [0] : tops;
  return {main: main && [main.left, main.right, main.top],
          aside: aside && [aside.left, aside.right, aside.top],
          rows: new Set(row).size};
})()"""


async def test_the_task_page_reads_as_a_column_with_details_beside_it(page: Page) -> None:
    """R1 stage 2: actions share one row; details sit beside the reading column."""
    run = await _any_task(page)
    await page.viewport(1440, 900)
    await show(page, f"task/{run}")
    await page.settle(1.0)
    wide = await page.evaluate(PLACES)
    assert wide["main"] and wide["aside"], wide
    assert wide["aside"][0] >= wide["main"][1], "details beside the reading column"
    assert wide["main"][1] - wide["main"][0] <= 48 * 16, "the reading column keeps a measure"
    assert wide["rows"] == 1, "actions share one row"
    await page.viewport(390, 844)
    await page.settle(1.0)
    narrow = await page.evaluate(PLACES)
    assert narrow["aside"][2] >= narrow["main"][2], "on a phone the details come after"
    await page.viewport(1440, 900)


async def test_a_long_activity_entry_folds_and_unfolds(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    folded = await page.evaluate(
        """(() => {
          const holder = document.createElement('div');
          holder.innerHTML = foldable('x'.repeat(900), 'fold-test');
          document.querySelector('#content').appendChild(holder);
          const text = holder.querySelector('#fold-test');
          const before = text.classList.contains('folded');
          Office.actions.unfold('fold-test');
          const after = text.classList.contains('folded');
          const short = foldable('short', 'fold-short');
          holder.remove();
          return {before, after, short};
        })()"""
    )
    assert folded["before"] is True and folded["after"] is False
    assert "folded" not in folded["short"] and "unfold" not in folded["short"]


async def test_the_desk_offers_one_place_to_start(page: Page) -> None:
    """TF1: talk first. One box to say anything; no task form in the way."""
    await page.viewport(1440, 900)
    await show(page, "home")
    boxes = await page.evaluate(
        "[...document.querySelectorAll('#content [data-start-topic]')]"
        ".filter((el) => el.offsetParent !== null).length"
    )
    assert boxes == 1
    forms = await page.evaluate(
        "[...document.querySelectorAll('#content [data-action=\"new-task\"]')]"
        ".filter((el) => el.offsetParent !== null).length"
    )
    assert forms == 0, "the desk starts with talk, not a task form"


async def test_a_room_step_with_nothing_to_show_has_no_empty_card(page: Page) -> None:
    await show(page, "home")
    card = await page.evaluate("roomCard({stage: 'engineering'}, 'run-1')")
    assert card == ""
    said = await page.evaluate(
        "roomItem({kind: 'step', run_id: 'run-1', task: 'A long task title', text: 'Done.',"
        " at: new Date().toISOString(), author: {name: 'Engineer', kind: 'agent'},"
        " card: {stage: 'engineering'}})"
    )
    assert 'href="#task/run-1"' in said, "the task is named once, as a link"


async def test_board_filters_sit_on_one_row(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "board")
    tops = await page.evaluate(
        "[...document.querySelectorAll('.board-filters select')]"
        ".map((el) => Math.round(el.getBoundingClientRect().top))"
    )
    assert len(tops) == 3 and max(tops) - min(tops) < 4, tops
    clipped = await page.evaluate(
        "[...document.querySelectorAll('.board-card')]"
        ".filter((el) => el.scrollWidth > el.clientWidth + 1).length"
    )
    assert clipped == 0, "a board card is wider than it shows"
    wrapped = await page.evaluate(
        "[...document.querySelectorAll('.board-head h2')]"
        ".filter((el) => el.getBoundingClientRect().height > 30).map((el) => el.textContent)"
    )
    assert wrapped == [], wrapped


async def test_phone_navigation_is_labelled(page: Page) -> None:
    """UI-3: on a phone, four places in a bar at the bottom, each named,
    the current one marked; nothing scrolls sideways."""
    await page.viewport(390, 844)
    await show(page, "home")
    await page.settle(1.0)
    bar = await page.evaluate(
        "[...document.querySelectorAll('.phone-tab')]"
        ".filter((a) => a.offsetParent !== null)"
        ".map((a) => [a.textContent.replace(/\\d+$/, '').trim(), a.classList.contains('active'),"
        " Math.round(a.getBoundingClientRect().bottom)])"
    )
    assert [label for label, _, _ in bar] == ["Desk", "Threads", "Library", "More"], bar
    assert [on for _, on, _ in bar] == [True, False, False, False]
    assert all(bottom >= 780 for _, _, bottom in bar), "the bar sits at the bottom"
    overflow = await page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert int(overflow) <= 2
    await page.viewport(1440, 900)
    hidden = await page.evaluate("getComputedStyle(document.querySelector('.phone-tabs')).display")
    assert hidden == "none", "a computer keeps its sidebar"


async def test_threads_and_more_are_one_tap_away_on_a_phone(page: Page) -> None:
    await page.viewport(390, 844)
    await show(page, "threads")
    await page.settle(1.5)
    listed = await page.evaluate("document.querySelectorAll('.threads-list .side-thread').length")
    sidebar = await page.evaluate(
        "api('/office/sidebar').then((s) => s.spaces.reduce((n, sp) => n + sp.threads.length, 0))"
    )
    assert listed == sidebar
    current = await page.evaluate("document.querySelector('.phone-tab.active')?.textContent")
    assert current == "Threads"
    await show(page, "more")
    await page.settle(1.0)
    places = await page.evaluate(
        "[...document.querySelectorAll('.more-place strong')].map((s) => s.textContent)"
    )
    assert places == ["Calendar", "Spaces", "Ideas", "Help", "Settings"], places
    await page.viewport(1440, 900)
    assert page.console == [], page.console


async def test_the_plan_tab_shows_the_chiefs_steps_and_where_each_stands(page: Page) -> None:
    """D1: a step-planned task shows its steps, their order and their status."""
    await show(page, "home")
    shown = await page.evaluate(
        """renderPlan({workflow_name: 'v0_analysis', state: {
            plan: {research_question: 'Which vendors?', steps: [
              {id: 'vendors', kind: 'research', brief: 'List eIDAS vendors', depends_on: []},
              {id: 'compare', kind: 'write', brief: 'Compare them', depends_on: ['vendors'],
               added_because: 'the critic asked for prices'}]},
            step_status: {vendors: 'done', compare: 'pending'}, replans: 1}}, [])"""
    )
    assert "List eIDAS vendors" in shown and "Compare them" in shown
    assert "Done" in shown and "Waiting" in shown and "after vendors" in shown
    assert "added because the critic asked for prices" in shown
    assert "revised 1 time" in shown


# Motion, Geist and the Island.


async def reduced_motion(page: Page, on: bool) -> None:
    features = [{"name": "prefers-reduced-motion", "value": "reduce" if on else "no-preference"}]
    await page.call("Emulation.setEmulatedMedia", {"features": features})


async def test_archivo_is_the_interface_font_and_is_served_by_the_office(page: Page) -> None:
    """The Floor set Archivo, as Geist was before it: served by the office."""
    await show(page, "home")
    await page.evaluate("document.fonts.ready.then(() => true)")
    family = await page.evaluate("getComputedStyle(document.body).fontFamily")
    assert family.lower().startswith('"archivo"') or family.lower().startswith("archivo"), family
    loaded = await page.evaluate("document.fonts.check('15px Archivo')")
    assert loaded is True
    sources = await page.evaluate(
        "performance.getEntriesByType('resource').map((r) => r.name)"
        ".filter((n) => n.includes('.woff2'))"
    )
    assert sources and all("/ui/fonts/" in name for name in sources), sources


async def test_a_spring_settles_on_its_target(page: Page) -> None:
    await show(page, "home")
    settled = await page.evaluate(
        """new Promise((done) => {
          const seen = [];
          Motion.spring({x: 0}, {x: 100}, (v) => seen.push(v.x),
            {stiffness: 400, damping: 30}).then(() => done({
              last: seen[seen.length - 1], over: Math.max(...seen) > 100, steps: seen.length}));
        })"""
    )
    assert abs(settled["last"] - 100) < 0.01, settled
    assert settled["steps"] > 3, "it animated rather than jumped"


async def test_reduced_motion_turns_motion_off(page: Page) -> None:
    await show(page, "home")
    await reduced_motion(page, True)
    try:
        jumped = await page.evaluate(
            """new Promise((done) => {
              const seen = [];
              Motion.spring({x: 0}, {x: 100}, (v) => seen.push(v.x)).then(() => done(seen));
            })"""
        )
        assert jumped == [100], "with reduced motion a spring lands at once"
        assert await page.evaluate("Motion.reduced()") is True
    finally:
        await reduced_motion(page, False)


PILL_FIT = """((selector, active) => {
  const pill = document.querySelector(selector + ' > .motion-pill');
  const target = document.querySelector(selector + ' ' + active);
  if (!pill || !target) return null;
  const a = pill.getBoundingClientRect(), b = target.getBoundingClientRect();
  return {dx: Math.abs(a.left - b.left), dy: Math.abs(a.top - b.top),
          dw: Math.abs(a.width - b.width), dh: Math.abs(a.height - b.height)};
})"""


async def test_the_selection_pill_springs_to_the_screen_you_chose(page: Page) -> None:
    """M1 stage 2: one pill per list, settling on what is selected."""
    await page.viewport(1440, 900)
    await show(page, "home")
    await show(page, "board")
    await page.settle(1.2)
    fit = await page.evaluate(f"{PILL_FIT}('#nav', 'a.active')")
    assert fit and max(fit.values()) < 2, fit
    moving = await page.evaluate(
        """new Promise((done) => {
          location.hash = 'settings';
          setTimeout(() => {
            const pill = document.querySelector('#nav > .motion-pill');
            const target = document.querySelector('#nav a.active');
            const a = pill.getBoundingClientRect(), b = target.getBoundingClientRect();
            // Down a side menu or along the top bar: either way it travels.
            done(Math.max(Math.abs(a.top - b.top), Math.abs(a.left - b.left)));
          }, 60);
        })"""
    )
    assert moving > 2, "the pill travels rather than jumping"
    await page.settle(1.2)
    fit = await page.evaluate(f"{PILL_FIT}('#nav', 'a.active')")
    assert fit and max(fit.values()) < 2, fit


async def test_tab_rows_have_a_pill_that_follows_the_active_tab(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "settings")
    await page.settle(1.0)
    fit = await page.evaluate(f"{PILL_FIT}('#content .subnav', 'button.active')")
    assert fit is not None and fit["dx"] < 2 and fit["dw"] < 2, fit


async def test_with_reduced_motion_the_pill_is_there_at_once(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    await reduced_motion(page, True)
    try:
        await page.evaluate("location.hash = 'tools'")
        await page.settle(0.4)
        fit = await page.evaluate(f"{PILL_FIT}('#nav', 'a.active')")
        assert fit and max(fit.values()) < 2, fit
    finally:
        await reduced_motion(page, False)


async def test_a_message_opens_the_island_and_it_springs_back(page: Page) -> None:
    """M1 stage 3: what toast said, the Island says; screen readers still hear it."""
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate("toast('Saved your limits.')")
    await page.settle(0.4)
    island = await page.evaluate(
        "({state: document.querySelector('#island').dataset.state,"
        " text: document.querySelector('#island').innerText,"
        " status: document.querySelector('#toast').textContent})"
    )
    assert island["state"] == "message" and "Saved your limits." in island["text"], island
    assert island["status"] == "Saved your limits.", "the status region still carries it"
    await page.settle(3.6)
    after = await page.evaluate("document.querySelector('#island').dataset.state")
    # It springs back to whatever it rests at: idle, work running, or a task
    # waiting on the owner (amber).
    assert after in ("idle", "live", "waiting"), after


async def test_the_island_shows_live_work_and_expands_into_it(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    # The office's own alerts would hold the island first; start it fresh.
    await page.evaluate("Island.reset()")
    await page.evaluate(
        "Island.live([{run_id: 'run-1', title: 'KYC vendor comparison', step: 'work'},"
        " {run_id: 'run-2', title: 'Pricing brief', step: 'research'}])"
    )
    await page.settle(0.8)
    live = await page.evaluate(
        "({state: document.querySelector('#island').dataset.state,"
        " text: document.querySelector('#island').innerText})"
    )
    assert live["state"] == "live" and "2 tasks" in live["text"], live
    await page.evaluate("document.querySelector('#island').click()")
    await page.settle(0.8)
    opened = await page.evaluate(
        "({state: document.querySelector('#island').dataset.state,"
        " links: [...document.querySelectorAll('#island a')].map((a) => a.getAttribute('href'))})"
    )
    assert opened["state"] == "expanded", opened
    assert "#task/run-1" in opened["links"] and "#task/run-2" in opened["links"], opened
    await page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
    await page.settle(0.6)
    assert await page.evaluate("document.querySelector('#island').dataset.state") == "live"
    await page.evaluate("Island.live([])")
    await page.settle(0.6)
    assert await page.evaluate("document.querySelector('#island').dataset.state") == "idle"


async def test_opening_a_screen_staggers_it_and_a_refresh_does_not(page: Page) -> None:
    """M1 stage 4: motion on what the owner did, never on the background refresh."""
    await page.viewport(1440, 900)
    await show(page, "home")
    entered = await page.evaluate(
        """new Promise((done) => {
          const content = document.querySelector('#content');
          const seen = new Set();
          const watch = new MutationObserver(() => {
            if (content.classList.contains('entering')) seen.add('entering');
          });
          watch.observe(content, {attributes: true, attributeFilter: ['class']});
          location.hash = 'tasks';
          setTimeout(() => { watch.disconnect(); done([...seen]); }, 1200);
        })"""
    )
    assert entered == ["entering"], "opening a screen plays its entrance"
    await page.settle(1.0)
    refreshed = await page.evaluate(
        """new Promise((done) => {
          const content = document.querySelector('#content');
          let played = false;
          const watch = new MutationObserver(() => {
            if (content.classList.contains('entering')) played = true;
          });
          watch.observe(content, {attributes: true, attributeFilter: ['class']});
          rerender().then(() => setTimeout(() => { watch.disconnect(); done(played); }, 300));
        })"""
    )
    assert refreshed is False, "a background refresh does not replay it"


async def test_a_skill_from_skills_sh_is_previewed_then_installed(page: Page) -> None:
    """K1: paste, read, choose the role, install; the network is stubbed in the page."""
    await page.viewport(1440, 900)
    await show(page, "skills")
    stub = """(() => {
      const real = api; window.__installed = null;
      window.api = (p, o) => {
        if (p === '/skills/ecosystem/preview') return Promise.resolve({
          source: 'acme/skills', path: 'skills/q/SKILL.md', commit: 'c'.repeat(40),
          name: 'to-questionnaire', description: 'Turn a gap into a questionnaire.',
          instructions: 'Ask who it goes to.', characters: 20,
          refused: ['scripts/fill.py'], omitted: [],
          assumptions: [{kind: 'files', text: 'It writes files.'}],
          audits: {gen: 'Pass', socket: 'Pass', snyk: 'Warn'}, permissions: [],
          suggested_role: 'summarist', skills_sh: 'https://www.skills.sh/acme/skills/q'});
        if (p === '/skills/ecosystem/install') {
          window.__installed = o.body;
          return Promise.resolve({});
        }
        return real(p, o);
      };
    })()"""
    await page.evaluate(stub)
    await page.evaluate("document.querySelector('[data-action=\"skill-source\"]').click()")
    await page.settle(0.6)
    await page.evaluate(
        "document.querySelector('#f-skill-source').value = 'acme/skills q';"
        " document.querySelector('[data-action=\"skill-source-read\"]').click()"
    )
    await page.settle(0.8)
    shown = await page.evaluate("document.querySelector('#skill-preview').innerText")
    assert "to-questionnaire" in shown and "scripts/fill.py" in shown
    assert "Snyk: Warn" in shown and "It writes files." in shown
    assert await page.evaluate("document.querySelector('#f-skill-role').value") == "summarist"
    await page.evaluate("document.querySelector('[data-action=\"skill-source-install\"]').click()")
    await page.settle(0.8)
    sent = await page.evaluate("window.__installed")
    assert sent == {"source": "acme/skills q", "commit": "c" * 40, "role": "summarist"}
    assert page.console == [], page.console


# Progress, spend and what needs you, at a glance.

PULSE = """((over) => Object.assign({
  running: [{run_id: 'r1', title: 'KYC vendor comparison', project: 'KYC', status: 'RUNNING',
    step: 'work', progress: {done: 2, total: 5, unit: 'steps'},
    budget: {share: 0.4, limit: 'cost', spent_cost: 0.4, max_cost: 1, spent_tokens: 10,
             max_tokens: 1000}}],
  waiting: [], finished: [], projects: [], alerts: [],
  month: {spent: 2, limit: 20, share: 0.1}}, over))"""


async def island_state(page: Page) -> dict:
    return await page.evaluate(
        "({state: document.querySelector('#island').dataset.state,"
        " waiting: document.querySelector('#island').dataset.waiting || '',"
        " text: document.querySelector('#island').innerText,"
        " rings: document.querySelectorAll('#island .ring').length,"
        " said: Island.history().map((m) => m.text)})"
    )


async def test_the_island_draws_progress_and_budget_rings(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate("Island.reset()")
    await page.evaluate(f"Island.pulse({PULSE}({{}}))")
    await page.settle(0.8)
    live = await island_state(page)
    assert live["state"] == "live" and live["rings"] >= 1, live
    await page.evaluate("document.querySelector('#island').click()")
    await page.settle(0.8)
    opened = await island_state(page)
    assert "2 of 5" in opened["text"] and "$0.40 of $1.00" in opened["text"], opened["text"]
    assert opened["rings"] >= 3, "progress, budget and the month"
    await page.evaluate("Island.collapse(); Island.pulse(" + PULSE + "({running: []}))")


async def test_the_island_announces_changes_not_the_first_reading(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate("Island.reset()")
    await page.evaluate(f"Island.pulse({PULSE}({{}}))")
    await page.settle(0.4)
    assert (await island_state(page))["said"] == [], "the first reading announces nothing"
    finished = (
        "{running: [], finished: [{run_id: 'r1', title: 'KYC vendor comparison',"
        " status: 'COMPLETED'}]}"
    )
    await page.evaluate(f"Island.pulse({PULSE}({finished}))")
    await page.settle(0.4)
    await page.evaluate(f"Island.pulse({PULSE}({finished}))")
    await page.settle(0.4)
    said = (await island_state(page))["said"]
    assert said == ["KYC vendor comparison finished."], said


async def test_waiting_turns_the_island_amber_and_says_so_once(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate("Island.reset()")
    await page.evaluate(f"Island.pulse({PULSE}({{running: []}}))")
    waiting = (
        "{running: [], waiting: [{run_id: 'r2', title: 'Vendor RFI',"
        " status: 'WAITING_FOR_APPROVAL', reason: 'Waiting for your approval'}]}"
    )
    await page.evaluate(f"Island.pulse({PULSE}({waiting}))")
    await page.settle(4.2)
    state = await island_state(page)
    assert state["waiting"] == "true", state
    assert state["said"] == ["Vendor RFI: waiting for your approval."], state["said"]
    await page.evaluate(f"Island.pulse({PULSE}({{running: []}}))")


async def test_a_limit_warning_is_said_once(page: Page) -> None:
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate("Island.reset(); localStorage.removeItem('office.island.seen')")
    alert = (
        "{running: [], alerts: [{id: 'a-80', scope: 'project', subject: 'p1',"
        " name: 'KYC', threshold: 80, share: 0.82, window: '2026-09'}]}"
    )
    await page.evaluate(f"Island.pulse({PULSE}({alert}))")
    await page.settle(0.3)
    await page.evaluate(f"Island.pulse({PULSE}({alert}))")
    await page.settle(0.3)
    said = (await island_state(page))["said"]
    assert said == ["KYC has used 82% of its monthly limit."], said


async def test_the_island_sound_switch_is_remembered(page: Page) -> None:
    await show(page, "home")
    await page.evaluate("localStorage.removeItem('office.island.sound')")
    assert await page.evaluate("Island.sound()") is False, "off by default"
    await page.evaluate("Island.sound(true)")
    assert await page.evaluate("localStorage.getItem('office.island.sound')") == "on"
    assert await page.evaluate("Island.sound()") is True
    await page.evaluate("Island.sound(false)")


async def test_model_health_and_balances_read_as_sentences(page: Page) -> None:
    """I1 stages 4-5: the Performance and Models tabs' new cards."""
    await show(page, "home")
    health = await page.evaluate(
        "modelHealthCards({days: 30, model_health: [{model: 'deepseek-flash', calls: 20,"
        " failures: 2, reliability: 0.9, common_failure: 'provider_timeout',"
        " p50_seconds: 40.2, p95_seconds: 190.5, tokens_per_second: 31.4}]})"
    )
    assert "deepseek-flash" in health and "90% answered" in health
    assert "mostly provider timeout" in health and "31.4 tokens/s" in health
    # Balances now sit on each provider's card.
    cards = await page.evaluate(
        "[{key: 'c1', balance: {state: 'ok', usable: true, currency: 'CNY', total: 12.3}},"
        " {key: 'c2', balance: {state: 'ok', limit: 10, remaining: 1.5}},"
        " {key: 'c3', balance: {state: 'unsupported',"
        " reason: 'OpenAI publishes usage and cost only to an admin key.'}}]"
        ".map(providerBalance).join(' | ')"
    )
    assert "12.30 CNY left" in cards and "$1.50 of $10.00 left" in cards
    assert "admin key" in cards
    await page.evaluate("Island.reset(); localStorage.removeItem('office.island.seen')")
    await page.evaluate(
        f"Island.pulse({PULSE}({{running: [], alerts: [{{id: 'p-80', scope: 'provider',"
        " subject: 'c1', name: 'DeepSeek', threshold: 80, share: 0.8, window: 'balance'}]}))"
    )
    await page.settle(0.3)
    assert await page.evaluate("Island.history().map((m) => m.text)") == [
        "DeepSeek is running low."
    ]
    assert page.console == [], page.console


async def test_the_island_opens_while_idle_so_its_sound_switch_is_reachable(page: Page) -> None:
    """Found reviewing the Handbook: the sound switch was only reachable while
    something ran, because the Island only opened then."""
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate("Island.reset(); Island.pulse(" + PULSE + "({running: []}))")
    await page.evaluate("document.querySelector('#island').click()")
    await page.settle(0.8)
    opened = await island_state(page)
    assert opened["state"] == "expanded", opened
    assert await page.evaluate("!!document.querySelector('#island [data-island-sound]')")
    await page.evaluate("Island.collapse()")


async def test_the_business_line_is_named_where_the_work_is_and_changed_in_place(
    page: Page,
) -> None:
    """The owner's report: nothing on a task or project said which business line
    it fell under, and placing one meant pressing Sort and reading a result."""
    await page.viewport(1440, 900)
    await show(page, "home")
    made = await page.evaluate(
        "(async () => {"
        " const tag = Math.random().toString(36).slice(2, 8);"
        " const line = await api('/ventures', {method: 'POST', body: {name: `Line ${tag}`}});"
        " const project = await api('/projects', {method: 'POST', body: {name: `Work ${tag}`}});"
        " return {line: line.id, name: line.name, project: project.id}; })()"
    )
    await show(page, f"project/{made['project']}")
    label = await page.evaluate("document.querySelector('[data-line-label]')?.innerText || ''")
    assert "No business line" in label, label

    await page.evaluate(
        f"document.querySelector('[data-action=\"line-change:{made['project']}\"]').click()"
    )
    await page.settle(1.0)
    await page.evaluate(
        f"(() => {{ const select = document.querySelector('#line-change-form #f-venture_id');"
        f" select.value = '{made['line']}';"
        " document.querySelector('#line-change-form').requestSubmit(); })()"
    )
    await page.settle(1.6)
    label = await page.evaluate("document.querySelector('[data-line-label]')?.innerText || ''")
    assert made["name"] in label and "your choice" in label, label

    # A task page says it too.
    run_id = await page.evaluate(
        "(async () => { const view = await api('/office/overview');"
        " return view.tasks[0]?.run_id || null; })()"
    )
    if run_id:
        await show(page, f"task/{run_id}")
        where = await page.evaluate("document.querySelector('.task-where')?.innerText || ''")
        assert where.startswith("In "), where
        assert await page.evaluate("!!document.querySelector('[data-line-label]')")
    assert page.console == [], page.console


async def test_a_task_opens_on_its_workflow_drawn_live(page: Page) -> None:
    """The owner asked for the workflow as a picture: who plans, what Jev judged,
    which helpers work now, review, and back to the Chief, with the log beneath."""
    await page.viewport(1440, 900)
    await show(page, "home")
    run_id = await page.evaluate(
        "(async () => { const view = await api('/office/overview');"
        " return view.tasks[0]?.run_id || null; })()"
    )
    if not run_id:
        pytest.skip("no task in this office")
    await page.evaluate("TaskRoom.tab = 'workflow'")
    await show(page, f"task/{run_id}")
    drawn = await page.evaluate(
        "(() => { const map = document.querySelector('[data-flow]');"
        " if (!map) return null;"
        " return { lanes: ['.wf-chief', '.wf-jev', '.wf-workers', '.wf-critic', '.wf-answer']"
        "   .filter((s) => map.querySelector(s)).length,"
        "  steps: map.querySelectorAll('[data-flow-step]').length,"
        "  log: !!map.querySelector('.wf-log'), status: !!map.querySelector('.wf-status'),"
        "  advisor: !!map.querySelector('[data-advisor-lane]') }; })()"
    )
    assert drawn is not None, "the Workflow tab is not the first thing a task shows"
    assert drawn["lanes"] == 5 and drawn["log"] and drawn["status"], drawn
    assert drawn["advisor"], "the advisor's lane is on the map, on call or off"
    flow = await page.evaluate(f"api('/runs/{run_id}/flow')")
    assert drawn["steps"] == len(flow["steps"])

    # At phone width the map wraps rather than pushing the page sideways.
    await page.viewport(390, 844)
    await page.settle(0.8)
    wide = await page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    assert not wide, "the workflow map overflows a phone screen"
    assert page.console == [], page.console


async def test_the_advisor_is_turned_on_and_off_in_settings(page: Page) -> None:
    await page.viewport(1440, 900)
    await page.evaluate("Settings.tab = 'models'")
    await show(page, "settings")
    card = await page.evaluate("document.querySelector('[data-advisor-card]')?.innerText || ''")
    assert "Advisor on call" in card, card
    before = await page.evaluate("api('/settings').then((s) => s.settings.advisor_on)")
    await page.evaluate("document.querySelector('[data-action^=\"advisor-switch:\"]').click()")
    await confirm_widening(page)
    await page.settle(1.2)
    after = await page.evaluate("api('/settings').then((s) => s.settings.advisor_on)")
    assert after is (not before)
    await page.evaluate(
        "api('/settings', {method: 'PUT', confirmed: true, body: "
        f"{{advisor_on: {str(before).lower()}}}}})"
    )
    assert page.console == [], page.console


async def test_the_answer_is_the_newest_final_answer_with_the_report_beneath(page: Page) -> None:
    """The owner's report: a reworked task wrote a new result, and the page went
    on showing the old one. The page led with the writer's report; the Chief's
    final answer, written last, sat unread under Artifacts."""
    await page.viewport(1440, 900)
    await show(page, "home")
    found = await page.evaluate(
        """(async () => {
             const view = await api('/office/overview');
             for (const task of view.tasks.filter((t) => t.status === 'COMPLETED').slice(0, 12)) {
               const artifacts = await api(`/runs/${task.run_id}/artifacts`);
               const finals = artifacts.filter((a) => a.type === 'synthesis');
               const reports = artifacts.filter((a) => a.type === 'strategy_proposal');
               if (finals.length && reports.length) {
                 return { run: task.run_id, final: finals[finals.length - 1].id };
               }
             }
             return null;
           })()"""
    )
    if not found:
        pytest.skip("no finished task here has both a final answer and a report")
    await show(page, f"task/{found['run']}")
    await page.settle(2.0)
    shown = await page.evaluate("document.querySelector('.card.answer')?.dataset.answerId || ''")
    assert shown == found["final"], "the answer card is not the newest final answer"
    assert await page.evaluate("!!document.querySelector('.card.answer [data-report]')")
    assert page.console == [], page.console


async def test_a_task_stopped_by_review_shows_the_critics_concerns(page: Page) -> None:
    """The owner's report: the task said the critic kept raising the same
    concerns, and did not say which, so there was nothing to review."""
    await page.viewport(1440, 900)
    await show(page, "home")
    run_id = await page.evaluate(
        """(async () => {
             const view = await api('/office/overview');
             for (const task of view.tasks.filter((t) => t.status === 'NEEDS_HUMAN_REVIEW')) {
               const run = await api(`/runs/${task.run_id}`);
               const stopped = run.state?.stopped_reason === 'revision_not_converging';
               if (run.state?.last_critique && stopped) return run.id;
             }
             return null;
           })()"""
    )
    if not run_id:
        pytest.skip("no task here was stopped by a critic that kept repeating itself")
    await show(page, f"task/{run_id}")
    card = await page.evaluate(
        "(() => { const c = document.querySelector('[data-critic-review]');"
        " if (!c) return null;"
        " return { text: c.innerText, boxes: c.querySelectorAll('[data-concern]').length }; })()"
    )
    assert card is not None, "the critic's concerns are not on the page"
    assert "How to decide" in card["text"] and "Finish with what it has" in card["text"]
    critique = await page.evaluate(f"api('/runs/{run_id}').then((r) => r.state.last_critique)")
    expected = sum(
        len(critique.get(key) or [])
        for key in ("required_corrections", "critical_issues", "missing_evidence")
    )
    assert card["boxes"] == expected
    await page.evaluate(
        "document.querySelectorAll('[data-critic-review] [data-concern]')"
        ".forEach((box, i) => { box.checked = i < 2; })"
    )
    said = await page.evaluate("reviewComment()")
    assert said.startswith("Fix these concerns from the critic:")
    assert critique["required_corrections"][0] in said
    assert page.console == [], page.console


async def test_a_skill_says_where_it_is_offered_and_when_it_has_an_update(page: Page) -> None:
    """K2: an improved skill says so; a business line keeps its own pack."""
    await page.viewport(1440, 900)
    await show(page, "home")
    updates = await page.evaluate("api('/skills/updates')")
    available = [u for u in updates if u["state"] == "available"]
    await page.evaluate("AgentState.skillTab = 'installed'")
    await show(page, "skills")
    body = await text(page)
    installed = await page.evaluate("api('/skills')")
    if not installed:
        pytest.skip("no skills installed here")
    assert await page.evaluate("document.querySelectorAll('[data-skill-lines]').length") > 0
    assert "Offered" in body
    if available:
        assert "Update available" in body
        assert await page.evaluate("!!document.querySelector('[data-skill-update] button')")
    # The business line form offers the installed skills as its pack.
    await show(page, "projects")
    await page.evaluate("document.querySelector('[data-action=\"venture-new\"]').click()")
    await page.settle(1.0)
    form = await page.evaluate("document.querySelector('#venture-form')?.innerText || ''")
    assert "Skills for this line" in form
    assert (
        await page.evaluate("document.querySelectorAll('#venture-form [name=skills]').length") > 0
    )
    await page.evaluate("closeModal()")
    assert page.console == [], page.console


async def test_a_project_is_renamed_from_its_page(page: Page) -> None:
    """The owner's report: a project's name could not be changed."""
    await page.viewport(1440, 900)
    await show(page, "home")
    made = await page.evaluate(
        "api('/projects', {method: 'POST', body: {name: `Old ${Date.now()}`}})"
    )
    await show(page, f"project/{made['id']}")
    await page.evaluate("document.querySelector('[data-action=\"project-edit\"]').click()")
    await page.settle(0.8)
    await page.evaluate(
        "(() => { const form = document.querySelector('#project-edit-form');"
        " form.querySelector('[name=name]').value = 'Renamed project';"
        " form.requestSubmit(); })()"
    )
    await page.settle(1.6)
    title = await page.evaluate("document.querySelector('.heading h1')?.innerText || ''")
    assert title == "Renamed project", title
    assert page.console == [], page.console


async def test_an_agent_is_created_from_the_agents_screen(page: Page) -> None:
    """The owner's report: the button to create an agent did nothing."""
    await page.viewport(1440, 900)
    await show(page, "agents")
    await page.evaluate("document.querySelector('[data-action=\"new-agent\"]').click()")
    await page.settle(1.5)
    state = await page.evaluate(
        "(() => { const form = document.querySelector('#agent-create-form');"
        " if (!form) return {form: false};"
        " form.querySelector('[name=name]').value = `Agent ${Date.now()}`;"
        " const submit = [...form.querySelectorAll('button')]"
        "   .find((b) => /Create/.test(b.innerText));"
        " const rect = submit?.getBoundingClientRect();"
        " const at = rect ? document.elementFromPoint("
        "   rect.x + rect.width / 2, rect.y + rect.height / 2) : null;"
        " return {form: true, submit: !!submit, onTop: at === submit || submit?.contains(at),"
        "  covering: at && at !== submit ? (at.id || at.className || at.tagName) : null,"
        "  invalid: [...form.querySelectorAll(':invalid')].map((e) => e.name)}; })()"
    )
    assert state["form"] and state["submit"], state
    assert state["onTop"], f"something covers the Create button: {state}"
    assert state["invalid"] == [], state
    await page.evaluate(
        "[...document.querySelectorAll('#agent-create-form button')]"
        ".find((b) => /Create/.test(b.innerText)).click()"
    )
    await page.settle(2.0)
    where = await page.evaluate("location.hash")
    assert where.startswith("#agent/"), f"no agent was created: {where}"
    assert page.console == [], page.console


async def test_a_tall_dialogs_buttons_stay_on_screen(page: Page) -> None:
    """The owner's report: Create agent could not be clicked. On a laptop-height
    window the dialog was taller than the screen and its buttons sat below it."""
    await page.viewport(1280, 700)
    await show(page, "agents")
    await page.evaluate("document.querySelector('[data-action=new-agent]').click()")
    await page.settle(1.5)
    reach = await page.evaluate(
        "(() => { const f = document.querySelector('#agent-create-form');"
        " const s = [...f.querySelectorAll('button')].find((b) => /Create/.test(b.innerText));"
        " const r = s.getBoundingClientRect();"
        " const at = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);"
        " return {bottom: r.bottom, view: innerHeight, onTop: at === s || s.contains(at)}; })()"
    )
    assert reach["bottom"] <= reach["view"] and reach["onTop"], reach
    await page.evaluate("closeModal()")
    assert page.console == [], page.console


async def test_an_agent_with_a_role_written_in_words_is_created(page: Page) -> None:
    """The owner's report: "Slac", as "slac communication assistant", was
    refused each time with a regular expression for an explanation."""
    await page.viewport(1280, 700)
    await show(page, "agents")
    await page.evaluate("document.querySelector('[data-action=new-agent]').click()")
    await page.settle(1.5)
    shown = await page.evaluate(
        "(() => { const f = document.querySelector('#agent-create-form');"
        " f.querySelector('[name=name]').value = `Slac ${Date.now()}`;"
        " const role = f.querySelector('[name=role]'); role.value = '__custom';"
        " role.dispatchEvent(new Event('change', {bubbles: true}));"
        " const custom = f.querySelector('[name=custom_role]');"
        " custom.value = 'Slac communication assistant';"
        " custom.dispatchEvent(new Event('input', {bubbles: true}));"
        " return document.querySelector('#custom-role-saved').innerText; })()"
    )
    assert shown == "Saved as: slac-communication-assistant", shown
    await page.evaluate(
        "[...document.querySelectorAll('#agent-create-form button')]"
        ".find((b) => /Create/.test(b.innerText)).click()"
    )
    await page.settle(2.0)
    where = await page.evaluate("location.hash")
    assert where.startswith("#agent/"), f"the agent was not created: {where}"
    assert page.console == [], page.console


async def test_projects_and_tasks_show_progress_cards(page: Page) -> None:
    """P1: a card per active project and per task in motion, readable on a phone."""
    await page.viewport(1440, 900)
    await show(page, "home")
    seen = await page.evaluate("api('/office/progress')")
    await show(page, "projects")
    shown = await page.evaluate("document.querySelectorAll('[data-progress-card=project]').length")
    active = await page.evaluate(
        "api('/projects').then((p) => p.filter((x) => x.status === 'ACTIVE').length)"
    )
    assert shown == active, (shown, active)
    if seen["projects"]:
        first = seen["projects"][0]
        ring = await page.evaluate(
            "document.querySelector("
            f"'[data-progress-card=project][data-id=\"{first['id']}\"] [data-ring]').dataset.ring"
        )
        expected = "" if first["ring"] is None else str(first["ring"])
        assert ring in (expected, expected.rstrip("0").rstrip(".")), (ring, first["ring"])
    await show(page, "tasks")
    cards = await page.evaluate("document.querySelectorAll('[data-progress-card=task]').length")
    assert cards == len(seen["tasks"])
    await page.viewport(390, 844)
    await page.settle(0.8)
    wide = await page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    assert not wide, "the cards overflow a phone screen"
    assert page.console == [], page.console


async def test_settings_says_what_the_office_can_reach(page: Page) -> None:
    """RE1: each source, whether it works, and how to fix what does not."""
    await page.viewport(1440, 900)
    await page.evaluate("Settings.tab = 'reach'")
    await show(page, "settings")
    rows = await page.evaluate(
        "Array.from(document.querySelectorAll('[data-reach]')).map((r) => r.dataset.reach)"
    )
    assert "search" in rows and "tools" in rows, rows
    body = await text(page)
    assert "What the office can reach" in body
    await page.evaluate("document.querySelector('[data-action=\"reach-check\"]').click()")
    await page.settle(2.0)
    assert await page.evaluate("document.querySelectorAll('[data-reach]').length") == len(rows)
    await page.evaluate("Settings.tab = 'office'")
    assert page.console == [], page.console


async def test_the_catalog_is_searchable_and_adding_by_address_is_offered(page: Page) -> None:
    """The owner asked for more platforms, and for adding a server by hand."""
    await page.viewport(1440, 900)
    await show(page, "tools")
    await page.evaluate("document.querySelector('[data-action=\"mcp-catalog\"]').click()")
    await page.settle(1.5)
    total = await page.evaluate("document.querySelectorAll('.catalog > li').length")
    assert total >= 30, total
    await page.evaluate(
        "(() => { const f = document.querySelector('#catalog-filter');"
        " f.value = 'slack'; f.dispatchEvent(new Event('input')); })()"
    )
    shown = await page.evaluate(
        "Array.from(document.querySelectorAll('.catalog > li')).filter((li) => !li.hidden)"
        ".map((li) => li.querySelector('strong').innerText)"
    )
    assert shown == ["Slack"], shown
    await page.evaluate("document.querySelector('#modal [data-action=\"mcp-add\"]').click()")
    await page.settle(1.0)
    assert await page.evaluate("!!document.querySelector('#mcp-server-form [name=url]')")
    await page.evaluate("closeModal()")
    assert page.console == [], page.console


async def test_the_workflow_tab_shows_the_team_room_and_settings_turns_the_team_on(
    page: Page,
) -> None:
    """T: agents post in the task's room as they work; the owner turns team mode on."""
    await page.viewport(1440, 900)
    await show(page, "home")
    run_id = await page.evaluate(
        "(async () => { const view = await api('/office/overview');"
        " for (const t of view.tasks.slice(0, 20)) {"
        "  const f = await api(`/runs/${t.run_id}/flow`);"
        # A room with a wave in it: tasks other checks start may only have an answer.
        "  if (f.room && (f.room.posts || []).some((p) => p.phase === 'work'))"
        "   return t.run_id; } return null; })()"
    )
    if run_id:
        await page.evaluate("TaskRoom.tab = 'room'")
        await show(page, f"task/{run_id}")
        room = await page.evaluate(
            "(() => { const r = document.querySelector('[data-team-room]');"
            " return r ? r.querySelectorAll('.wf-post').length : -1; })()"
        )
        assert room > 0, "the room is not on its tab"
        tab = await page.evaluate(
            "document.querySelector('[data-action=\"tab:room\"]')?.innerText || ''"
        )
        assert tab.startswith("Team room"), tab
        stages = await page.evaluate(
            "Array.from(document.querySelectorAll('[data-team-room] .wf-stage > summary'))"
            ".map((s) => s.innerText)"
        )
        assert stages and any("Wave 1" in s for s in stages), stages
        # A stage folded away stays folded when the task's page refreshes.
        await page.evaluate(
            "(() => { const d = document.querySelector('[data-team-room] .wf-stage');"
            " d.open = false; })()"
        )
        await page.settle(0.3)
        await show(page, f"task/{run_id}")
        folded = await page.evaluate("document.querySelector('[data-team-room] .wf-stage')?.open")
        assert folded is False
        await page.evaluate("ROOM_FOLDED.clear()")
    await page.evaluate("Settings.tab = 'models'")
    await show(page, "settings")
    card = await page.evaluate("document.querySelector('[data-team-card]')?.innerText || ''")
    assert "Agents as a team" in card
    places = await page.evaluate("document.querySelector('[data-runs-now]')?.innerText || ''")
    assert places.startswith("Running "), places
    before = await page.evaluate("api('/settings').then((s) => s.settings.team_on)")
    await page.evaluate("document.querySelector('[data-action^=\"team-switch:\"]').click()")
    await page.settle(1.2)
    after = await page.evaluate("api('/settings').then((s) => s.settings.team_on)")
    assert after is (not before)
    await page.evaluate(
        f"api('/settings', {{method: 'PUT', body: {{team_on: {str(before).lower()}}}}})"
    )
    await page.evaluate("Settings.tab = 'office'")
    assert page.console == [], page.console


async def test_the_owner_posts_to_a_step_in_the_room(page: Page) -> None:
    """T4: the owner speaks in the room while the task works."""
    await page.viewport(1440, 900)
    await show(page, "home")
    run_id = await page.evaluate(
        "(async () => { const view = await api('/office/overview');"
        " for (const t of view.tasks.filter((x) => ['RUNNING', 'PENDING'].includes(x.status))) {"
        "  const f = await api(`/runs/${t.run_id}/flow`);"
        "  if ((f.steps || []).some((s) => s.brief)) return t.run_id; } return null; })()"
    )
    if not run_id:
        pytest.skip("no working task with a plan here")
    await page.evaluate("TaskRoom.tab = 'room'")
    await show(page, f"task/{run_id}")
    before = await page.evaluate(f"api('/runs/{run_id}/flow').then((f) => f.room.count)")
    target = await page.evaluate(
        "Array.from(document.querySelectorAll('#room-post-form [name=to] option'))"
        ".map((o) => o.value).find((v) => v.startsWith('step:')) || 'team'"
    )
    await page.evaluate(
        "(() => { const f = document.querySelector('#room-post-form');"
        f" f.querySelector('[name=to]').value = '{target}';"
        " f.querySelector('[name=text]').value = 'Use the 2026 figures.';"
        " f.requestSubmit(); })()"
    )
    await page.settle(1.6)
    room = await page.evaluate(f"api('/runs/{run_id}/flow').then((f) => f.room)")
    assert room["count"] == before + 1
    last = room["posts"][-1]
    assert last["kind"] == "owner" and last["text"] == "Use the 2026 figures."
    assert last["to_step"] == (target[5:] if target.startswith("step:") else None)
    shown = await page.evaluate("document.querySelector('[data-team-room]').innerText")
    assert "Use the 2026 figures." in shown
    assert page.console == [], page.console


async def test_the_owner_asks_about_a_task_from_its_team_room(page: Page) -> None:
    """TR: one room. A question asked on a task's Team room tab is answered there
    and shows in the project's room, named with the task."""
    await page.viewport(1440, 900)
    await show(page, "home")
    found = await page.evaluate(
        "(async () => { const view = await api('/office/overview');"
        " for (const t of view.tasks.slice(0, 20)) {"
        "  const run = await api(`/runs/${t.run_id}`);"
        "  if (run.project_id) return [t.run_id, run.project_id]; } return null; })()"
    )
    if not found:
        pytest.skip("no task in a project here")
    run_id, project_id = found
    await page.evaluate("TaskRoom.tab = 'room'")
    await show(page, f"task/{run_id}")
    asked = await page.evaluate(
        "Array.from(document.querySelectorAll('#room-post-form [name=to] option'))"
        ".map((o) => o.value)"
    )
    assert asked and asked[0] == "ask", asked
    await page.evaluate(
        "(() => { const f = document.querySelector('#room-post-form');"
        " f.querySelector('[name=text]').value = 'Why was AutoGen left out?';"
        " f.requestSubmit(); })()"
    )
    await page.settle(6.0)
    talk = await page.evaluate("document.querySelector('[data-room-talk]')?.innerText || ''")
    assert "Why was AutoGen left out?" in talk, talk
    assert "answered" in talk, "an answer (or the office saying why not) comes back"
    await show(page, f"room/{project_id}")
    await page.settle(2.0)
    feed = await page.evaluate("document.querySelector('#room-feed')?.innerText || ''")
    assert "Why was AutoGen left out?" in feed and "about" in feed, feed[-400:]
    assert page.console == [], page.console


async def test_the_room_is_a_chat_with_replies_and_threads(page: Page) -> None:
    """CH: yours on the right, others on the left; reply to one message; a thread.

    Every message names an agent no office has, so the office answers for
    itself: nothing is spent. It makes its own project and removes it.
    """
    await page.viewport(1440, 900)
    name = f"Browser chat {await page.evaluate('Date.now()')}"
    project_id = await page.evaluate(
        f"api('/projects', {{method: 'POST', body: {{name: '{name}'}}}}).then((p) => p.id)"
    )

    async def send(form: str, box: str, words: str) -> None:
        await page.evaluate(
            f"(() => {{ const t = document.querySelector('{box}'); t.value = '{words}';"
            f" document.querySelector('{form}').requestSubmit(); }})()"
        )
        await page.settle(3.0)

    try:
        await page.evaluate(f"location.hash = 'room/{project_id}'")
        await page.settle(1.8)
        await send("#room-form", "#room-text", "@Nobody-here first")
        sides = await page.evaluate(
            "[...document.querySelectorAll('#room-feed .room-message')]"
            ".map((m) => [m.classList.contains('mine'),"
            " m.querySelector('.room-bubble').getBoundingClientRect().left])"
        )
        assert sides[0][0] is True and sides[1][0] is False, sides
        assert sides[0][1] > sides[1][1], "yours sits to the right of the office's"
        # Your words are readable on your bubble: not its colour.
        colours = await page.evaluate(
            "(() => { const b = document.querySelector("
            "'#room-feed .room-message.mine .room-bubble');"
            " return [getComputedStyle(b).backgroundColor,"
            " getComputedStyle(b.querySelector('.room-text')).color]; })()"
        )
        assert colours[0] != colours[1], f"your message text is the bubble's colour: {colours}"
        # Reply to the office's note: the box says so, and the message quotes it.
        office_id = await page.evaluate(
            "document.querySelector('#room-feed .room-message.office').dataset.mid"
        )
        await page.evaluate(
            f"document.querySelector('[data-action=\"room-reply:{office_id}\"]').click()"
        )
        chip = await page.evaluate("document.querySelector('#room-reply-to').innerText")
        assert "Replying to" in chip, chip
        await send("#room-form", "#room-text", "@Nobody-here about that")
        quote = await page.evaluate(
            "[...document.querySelectorAll('#room-feed .room-message.mine')].pop()"
            ".querySelector('.room-quote')?.innerText || ''"
        )
        assert "There is no agent" in quote, quote
        # A thread on the first message: replies there, a count here.
        first_id = await page.evaluate(
            "document.querySelector('#room-feed .room-message.mine').dataset.mid"
        )
        await page.evaluate(
            f"document.querySelector('[data-action=\"room-thread:{first_id}\"]').click()"
        )
        assert await page.evaluate("!!document.querySelector('#room-thread')")
        await send("#room-thread-form", "#room-thread-text", "@Nobody-here in the thread")
        thread = await page.evaluate("document.querySelector('#room-thread-feed').innerText")
        assert "in the thread" in thread, thread
        feed = await page.evaluate("document.querySelector('#room-feed').innerText")
        assert "in the thread" not in feed, "a thread's replies stay in the thread"
        link = await page.evaluate(
            "document.querySelector('#room-feed .room-thread-link')?.innerText || ''"
        )
        assert link.startswith("2 replies"), link
        await page.evaluate("document.querySelector('[data-action=\"room-thread-close\"]').click()")
        assert not await page.evaluate("!!document.querySelector('#room-thread')")
        assert page.console == [], page.console
    finally:
        await page.evaluate(
            f"api('/projects/{project_id}', {{method: 'DELETE', body: {{confirm: '{name}'}}}})"
        )


async def test_a_task_shows_its_line_of_work(page: Page) -> None:
    """B4.1: what a task built on and what built on it, on its page."""
    await page.viewport(1440, 900)
    await show(page, "home")
    run_id = await page.evaluate(
        "(async () => { const view = await api('/office/overview');"
        " for (const t of view.tasks.slice(0, 30)) {"
        "  const l = await api(`/runs/${t.run_id}/lineage`);"
        "  if (l.built_on.length) return t.run_id; } return null; })()"
    )
    if not run_id:
        pytest.skip("no follow-up here")
    await show(page, f"task/{run_id}")
    card = await page.evaluate("document.querySelector('[data-lineage-card]')?.innerText || ''")
    assert "This line of work" in card and "Continues" in card, card
    assert page.console == [], page.console


async def test_work_handed_off_in_a_discussion_shows_and_comes_back(page: Page) -> None:
    """TF2: a hand-off shows as a live card where it was asked; the result comes back.

    Drawn from data it supplies, not whatever an office happens to hold: it
    once read the first discussion with work in it and asserted that one."""
    await page.viewport(1440, 900)
    await show(page, "home")
    card = await page.evaluate(
        "roomWorkCard({run_id: 'r1', title: 'Compare the prices', status: 'RUNNING',"
        " steps: 3, steps_done: 1, spent_usd: 0.12, max_cost_usd: 0.5})"
    )
    assert "Work" in card and "Step 2 of 3" in card and "Pause" in card, card
    assert "the result comes back here" in card
    delivered = await page.evaluate(
        "roomDelivery({run_id: 'r1', at: new Date().toISOString(),"
        " text: 'Three agencies publish prices.'})"
    )
    assert "Delivered" in delivered and "Three agencies" in delivered, delivered
    offer = await page.evaluate(
        "roomMessageActions({id: 'm:7', actions: [{kind: 'start_work', objective: 'Compare'}]})"
    )
    assert offer.count("room-start-now:") == 1, "an unsure hand-off is one button"
    assert page.console == [], page.console


async def test_a_colleague_is_brought_in_and_a_message_branches(page: Page) -> None:
    """TF3: bring someone into a discussion; branch a tangent into its own topic."""
    await page.viewport(1440, 900)
    await show(page, "home")
    topic_id = await page.evaluate(
        "api('/topics', {method: 'POST', body: {text: '@Nobody-here pricing a first service'}})"
        ".then((r) => r.topic.id)"
    )
    stamp = await page.evaluate("Date.now()")
    agent_id = await page.evaluate(
        f"api('/agents', {{method: 'POST', body: {{name: 'Mia{stamp}', role: 'researcher',"
        " instructions: 'Market research.'}}).then((a) => a.id)"
    )
    try:
        await show(page, f"topic/{topic_id}")
        await page.settle(1.0)
        await page.evaluate("document.querySelector('[data-action=\"topic-bring\"]').click()")
        await page.settle(0.6)
        await page.evaluate(
            f"(() => {{ const f = document.querySelector('#topic-bring-form');"
            f" f.querySelector('[name=profile_id]').value = '{agent_id}'; f.requestSubmit(); }})()"
        )
        await page.settle(2.5)
        feed = await page.evaluate("document.querySelector('#room-feed').innerText")
        assert f"Mia{stamp} joined" in feed, feed[-300:]
        owner_message = await page.evaluate(
            "document.querySelector('#room-feed .room-message.mine').dataset.mid"
        )
        await page.evaluate(
            f"document.querySelector('[data-action=\"topic-branch:{owner_message}\"]').click()"
        )
        await page.settle(0.6)
        await page.evaluate(
            "(() => { const f = document.querySelector('#topic-branch-form');"
            " f.querySelector('[name=title]').value = 'Sell through agencies';"
            " f.requestSubmit(); })()"
        )
        await page.settle(2.5)
        assert await page.evaluate("Office.view") == "topic"
        branched = await text(page)
        assert "Sell through agencies" in branched and "Branched from" in branched, branched[:300]
        card = await page.evaluate(
            "topicCard({id: 'x', title: 'T', brief: {where: 'Five compared.', decided: '',"
            " open: 'A call?'}, last_activity_at: new Date().toISOString()})"
        )
        assert "Where we were" in card and "Five compared." in card
        assert page.console == [], page.console
    finally:
        await page.evaluate(f"api('/agents/{agent_id}/archive', {{method: 'POST'}})")


async def test_the_desk_sorts_what_comes_back(page: Page) -> None:
    """TF4: needs you holds decisions with where they came from; returning
    discussions are parked (ideas have their own widget)."""
    await page.viewport(1440, 900)
    await show(page, "home")
    desk = await page.evaluate("api('/office/desk')")
    if not desk["needs"]:
        pytest.skip("no decision waiting here")
    needs = await page.evaluate(
        "document.querySelector('[aria-label=\"Needs you\"]')?.innerText || ''"
    )
    assert "From" in needs and desk["needs"][0]["from"]["title"] in needs, needs
    if desk["parked"]["returning"]:
        parked = await page.evaluate(
            "document.querySelector('[aria-label=\"Parked for later\"]')?.innerText || ''"
        )
        assert "Comes back" in parked
    assert page.console == [], page.console


async def test_a_thought_is_kept_on_enter_and_planned_for_a_day(page: Page) -> None:
    """ID1, ID2: the Ideas box keeps a thought on Enter, Plan for moves it to
    a day, Start asks before it spends, and `i` opens the box from any page."""
    await page.viewport(1440, 900)
    # The test office is a copy of the owner's; their wall may hold Ideas small.
    await page.evaluate("api('/widgets/ideas', {method: 'PATCH', body: {size: 'wide'}})")
    # The board by day (By kind is the default), kept unsorted.
    await page.evaluate(
        "localStorage.setItem('office.ideas.view', 'day');"
        " localStorage.removeItem('office.idea.kind'); Ideas.view = 'day'; Ideas.pick = ''"
    )
    await show(page, "home")
    words = f"Print button for the colouring site {uuid4().hex[:6]}"
    typed = await page.evaluate(
        "(() => { const box = document.querySelector('#idea-text');"
        f" if (!box) return false; box.value = {json.dumps(words)};"
        " box.form.requestSubmit(); return true; })()"
    )
    assert typed, "the Ideas widget has its box on the floor"
    await page.settle(1.6)
    row = await page.evaluate(
        "[...document.querySelectorAll('[data-idea]')]"
        f".find((r) => r.innerText.includes({json.dumps(words)}))?.dataset.bucket || ''"
    )
    assert row == "new", "kept under New, not yet planned"
    idea = await page.evaluate(
        f"(async () => (await api('/ideas')).new.find((i) => i.text === {json.dumps(words)}).id)()"
    )
    column = await page.evaluate(
        f"document.querySelector('[data-idea=\"{idea}\"]')?.closest('.idea-col')?.ariaLabel || ''"
    )
    assert column == "New", "a new idea sits in the New column of the board"
    await page.evaluate(f"document.querySelector('[data-action=\"idea-plan:{idea}\"]').click()")
    await page.settle(1.0)
    await page.evaluate(
        f"document.querySelector('#modal-content [data-action=\"idea-start:{idea}\"]').click()"
    )
    await page.settle(0.6)
    asked = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "Start this as work?" in asked and "$0.50" in asked, "it says the cost first"
    await page.evaluate("closeModal()")
    # One tap on the card: its second day button is Tomorrow.
    tomorrow = f"document.querySelector('[data-idea=\"{idea}\"] .idea-chip:nth-child(2)')"
    assert await page.evaluate(f"{tomorrow}.innerText") == "Tomorrow"
    await page.evaluate(f"{tomorrow}.click()")
    await page.settle(1.6)
    planned = await page.evaluate(
        "(async () => (await api('/ideas')).later"
        f".find((i) => i.id === {json.dumps(idea)})?.plan_for || '')()"
    )
    assert planned, "planned for tomorrow, so it waits under later"
    moved = await page.evaluate(
        f"document.querySelector('[data-idea=\"{idea}\"]')?.closest('.idea-col')?.ariaLabel || ''"
    )
    assert moved == "Coming up", "and the board shows it coming up"
    await show(page, "board")
    await page.press("i", "KeyI", 73)
    await page.settle(0.6)
    opened = await page.evaluate("document.activeElement?.id || ''")
    assert opened == "idea-quick-text", "i opens the box from another page"
    await page.evaluate("closeModal()")
    drop = "{method: 'POST', body: {status: 'dropped'}}"
    await page.evaluate(f"api('/ideas/{idea}/finish', {drop})")
    assert page.console == [], page.console


async def test_an_idea_is_placed_in_a_project_from_plan_for(page: Page) -> None:
    """Ideas: Plan for… holds the project and Block time; a project chosen
    there is the idea's."""
    await page.viewport(1440, 900)
    # The test office is a copy of the owner's; their wall may hold Ideas small.
    await page.evaluate("api('/widgets/ideas', {method: 'PATCH', body: {size: 'wide'}})")
    await show(page, "home")
    projects = await page.evaluate(
        "(async () => (await api('/projects')).filter((p) => p.status === 'ACTIVE'))()"
    )
    if not projects:
        pytest.skip("no project here to place an idea in")
    words = f"Idea for a project {uuid4().hex[:6]}"
    body = json.dumps({"text": words})
    idea = await page.evaluate(
        f"(async () => (await api('/ideas', {{method: 'POST', body: {body}}})).id)()"
    )
    await page.evaluate("rerender()")
    await page.settle(1.6)
    await page.evaluate(f"document.querySelector('[data-action=\"idea-plan:{idea}\"]').click()")
    await page.settle(1.0)
    dialog = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "Block time" in dialog and "Project" in dialog, dialog
    target = projects[0]
    await page.evaluate(
        "(() => { const pick = document.querySelector('#idea-project-pick');"
        f" pick.value = {json.dumps(target['id'])};"
        " pick.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )
    await page.settle(1.2)
    placed = await page.evaluate(
        "(async () => (await api('/ideas')).new"
        f".find((i) => i.id === {json.dumps(idea)})?.project?.name || '')()"
    )
    assert placed == target["name"]
    await page.evaluate("closeModal()")
    drop = "{method: 'POST', body: {status: 'dropped'}}"
    await page.evaluate(f"api('/ideas/{idea}/finish', {drop})")
    assert page.console == [], page.console


async def test_sort_my_ideas_says_its_cost_first_and_friday_asks(page: Page) -> None:
    """ID4: the Sort offer says it is one paid call before anything runs; on a
    Friday the board asks for what is not planned yet."""
    await page.viewport(1440, 900)
    # The test office is a copy of the owner's; their wall may hold Ideas small.
    await page.evaluate("api('/widgets/ideas', {method: 'PATCH', body: {size: 'wide'}})")
    await show(page, "home")
    made = []
    for words in ("Sort test one", "Sort test two"):
        body = json.dumps({"text": f"{words} {uuid4().hex[:6]}"})
        made.append(
            await page.evaluate(
                f"(async () => (await api('/ideas', {{method: 'POST', body: {body}}})).id)()"
            )
        )
    await page.evaluate("rerender()")
    await page.settle(1.6)
    await page.evaluate("document.querySelector('[data-action=\"ideas-sort\"]').click()")
    await page.settle(1.2)
    offer = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    if "Suggestions for your ideas" not in offer:
        assert "One model call" in offer and "Nothing changes until you accept" in offer
    await page.evaluate("closeModal()")
    banner = await page.evaluate(
        "(() => { const box = document.createElement('div'); box.innerHTML = ideasTile({desk:"
        " {ideas: {today_is: '2026-10-16', weekend: true, today: [], new: [{id: 'x',"
        " text: 'One', bucket: 'new'}], later: [], someday: [], counts: {new: 1}}}},"
        " {size: 'wide'}); return box.querySelector('.idea-friday')?.innerText || ''; })()"
    )
    assert "not planned yet" in banner
    drop = "{method: 'POST', body: {status: 'dropped'}}"
    for idea in made:
        await page.evaluate(f"api('/ideas/{idea}/finish', {drop})")
    assert page.console == [], page.console


async def test_watching_says_what_is_free_before_it_starts(page: Page) -> None:
    """WP6: the Watching widget is on the floor; its dialog says checks are free
    and changes are capped before the owner presses Start."""
    await page.viewport(1440, 900)
    await show(page, "home")
    tile = await page.evaluate(
        "document.querySelector('[aria-label=\"Watching\"]')?.innerText || ''"
    )
    assert "Watching" in tile.title() or "WATCHING" in tile
    await page.evaluate("document.querySelector('[data-action=\"watch-new\"]').click()")
    await page.settle(1.2)
    dialog = await page.evaluate("document.querySelector('#modal-content')?.innerText || ''")
    assert "Each check reads it for free" in dialog and "Start watching" in dialog
    await page.evaluate("closeModal()")
    assert page.console == [], page.console


async def test_the_thought_box_never_keeps_a_half_typed_word(page: Page) -> None:
    """10 Oct (owner, on a phone): no autocorrect, no zoom, and an Enter that
    confirms a pinyin character does not keep the thought."""
    await page.viewport(390, 844, 3.0)
    await show(page, "home")
    facts = await page.evaluate(
        "(() => { const box = document.querySelector('#idea-text'); if (!box) return null;"
        " return {tag: box.tagName, correct: box.getAttribute('autocorrect'),"
        " spell: box.getAttribute('spellcheck'),"
        " size: parseFloat(getComputedStyle(box).fontSize)}; })()"
    )
    assert facts is not None and facts["size"] >= 16, facts
    assert (facts["tag"], facts["correct"], facts["spell"]) == ("TEXTAREA", "off", "false")
    words = f"拼音测试 {uuid4().hex[:6]}"
    kept_while_composing = await page.evaluate(
        "(async () => { const box = document.querySelector('#idea-text');"
        f" box.value = {json.dumps(words)};"
        " box.dispatchEvent(new Event('input', {bubbles: true}));"
        " box.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true,"
        " bubbles: true, cancelable: true}));"
        " await new Promise((r) => setTimeout(r, 800));"
        f" return (await api('/ideas')).new.some((i) => i.text === {json.dumps(words)}); }})()"
    )
    assert kept_while_composing is False, "an Enter that picks a character keeps nothing"
    await page.evaluate(
        "document.querySelector('#idea-text').dispatchEvent(new KeyboardEvent('keydown',"
        " {key: 'Enter', bubbles: true, cancelable: true}))"
    )
    await page.settle(1.6)
    idea = await page.evaluate(
        "(async () => (await api('/ideas')).new"
        f".find((i) => i.text === {json.dumps(words)})?.id || '')()"
    )
    assert idea, "Enter, once composing is done, keeps it"
    drop = "{method: 'POST', body: {status: 'dropped'}}"
    await page.evaluate(f"api('/ideas/{idea}/finish', {drop})")
    await page.viewport(1440, 900)
    assert page.console == [], page.console


async def test_a_task_is_renamed_on_a_phone(page: Page) -> None:
    """10 Oct: a task's title could not be changed, least of all on a phone."""
    await page.viewport(390, 844, 3.0)
    await show(page, "home")
    run = await page.evaluate(
        "(async () => { const runs = await (await fetch('/runs?limit=5',"
        " {credentials: 'same-origin'})).json();"
        " const rows = Array.isArray(runs) ? runs : (runs.items || runs.runs || []);"
        " return rows[0] ? {id: rows[0].id, title: rows[0].title} : null; })()"
    )
    if not run:
        pytest.skip("no task in this office to rename")
    await show(page, f"task/{run['id']}")
    await page.settle(1.0)
    button = await page.evaluate(
        "(() => { const b = document.querySelector('[data-action^=\"title-rename:task:\"]');"
        " if (!b) return null; const r = b.getBoundingClientRect();"
        " return {h: r.height, inView: r.right <= innerWidth}; })()"
    )
    assert button and button["h"] >= 44 and button["inView"], button
    await page.evaluate("document.querySelector('[data-action^=\"title-rename:task:\"]').click()")
    await page.settle(0.6)
    size = await page.evaluate(
        "parseFloat(getComputedStyle(document.querySelector('#f-rename-title')).fontSize)"
    )
    assert size >= 16, "a phone does not zoom into the field"
    new_name = f"Renamed on a phone {uuid4().hex[:4]}"
    await page.evaluate(
        "(() => { const f = document.querySelector('#f-rename-title');"
        f" f.value = {json.dumps(new_name)}; f.form.requestSubmit(); }})()"
    )
    await page.settle(1.4)
    heading = await page.evaluate("document.querySelector('#content h1')?.innerText || ''")
    assert heading == new_name
    body = json.dumps({"title": run["title"] or ""})
    await page.evaluate(f"api('/runs/{run['id']}/title', {{method: 'PUT', body: {body}}})")
    topics = await page.evaluate("api('/topics')")
    if topics:
        topic = topics[0]
        await show(page, f"topic/{topic['id']}")
        await page.evaluate(
            "document.querySelector('[data-action^=\"title-rename:topic:\"]').click()"
        )
        await page.settle(0.6)
        await page.evaluate(
            "(() => { const f = document.querySelector('#f-rename-title');"
            f" f.value = {json.dumps(new_name)}; f.form.requestSubmit(); }})()"
        )
        await page.settle(1.4)
        assert (await page.evaluate(f"api('/topics/{topic['id']}')"))["title"] == new_name
        back = json.dumps({"title": topic["title"]})
        await page.evaluate(f"api('/topics/{topic['id']}', {{method: 'PUT', body: {back}}})")
    await page.viewport(1440, 900)
    assert page.console == [], page.console


async def test_on_a_phone_an_idea_is_a_tap_away(page: Page) -> None:
    """ID3: #idea is a screen of its own to bookmark, and a 💡 button on a
    narrow screen opens the quick box from any page."""
    await page.viewport(390, 844, 3.0)
    await show(page, "idea")
    words = f"Ask about the Q4 newsletter {uuid4().hex[:6]}"
    kept = await page.evaluate(
        "(() => { const box = document.querySelector('#idea-screen-text');"
        f" if (!box) return false; box.value = {json.dumps(words)};"
        " box.form.requestSubmit(); return true; })()"
    )
    assert kept, "the #idea screen has its own box"
    await page.settle(1.6)
    idea = await page.evaluate(
        "(async () => (await api('/ideas')).new"
        f".find((i) => i.text === {json.dumps(words)})?.id || '')()"
    )
    assert idea, "kept from the phone screen"
    await show(page, "board")
    fab = await page.evaluate(
        "getComputedStyle(document.querySelector('.idea-fab')).display !== 'none'"
    )
    assert fab, "the 💡 button shows on a narrow screen"
    await page.evaluate("document.querySelector('.idea-fab').click()")
    await page.settle(0.6)
    assert await page.evaluate("document.activeElement?.id || ''") == "idea-quick-text"
    await page.evaluate("closeModal()")
    drop = "{method: 'POST', body: {status: 'dropped'}}"
    await page.evaluate(f"api('/ideas/{idea}/finish', {drop})")
    await page.viewport(1440, 900)
    wide = await page.evaluate(
        "getComputedStyle(document.querySelector('.idea-fab')).display === 'none'"
    )
    assert wide, "on a wide screen the floor has the box, and `i`"
    assert page.console == [], page.console


async def test_the_panel_beside_a_discussion_follows_the_work(page: Page) -> None:
    """TF5: what the discussion left behind; the delivered document beside it,
    where a selected passage takes a comment."""
    await page.viewport(1440, 900)
    await show(page, "home")
    # A delivered task that was closed or stopped for review has no answer to
    # open, and rightly says so; this needs one that has.
    found = await page.evaluate(
        "(async () => { for (const t of await api('/topics')) {"
        "  const f = await api(`/topics/${t.id}/feed`);"
        "  for (const i of f.items.filter((i) => i.kind === 'delivery' && i.run_id)) {"
        "   if (await api(`/runs/${i.run_id}/answer`).catch(() => null))"
        "    return {topic: t.id, run: i.run_id};"
        "  }"
        " } return null; })()"
    )
    if not found:
        pytest.skip("no discussion with delivered work here")
    topic_id, run_id = found["topic"], found["run"]
    await show(page, f"topic/{topic_id}")
    await page.settle(1.5)
    await page.evaluate("document.querySelector('[data-action=\"dock-tab:board\"]').click()")
    await page.settle(1.0)
    rail = await page.evaluate("document.querySelector('#topic-rail')?.innerText || ''")
    assert "work from this thread" in rail.lower(), rail
    await page.evaluate(f"document.querySelector('[data-action=\"rail-doc:{run_id}\"]').click()")
    await page.settle(2.0)
    rail = await page.evaluate("document.querySelector('#topic-rail')?.innerText || ''")
    assert "Version 1" in rail, rail[:300]
    chosen = "document.querySelector('#dock-tab-draft').getAttribute('aria-selected')"
    assert await page.evaluate(chosen) == "true", "Open beside shows the Draft tab"
    await page.evaluate(
        "(() => { const doc = document.querySelector('#rail-doc');"
        " const range = document.createRange();"
        " const said = [...doc.querySelectorAll('p, li')]"
        "  .find((node) => node.textContent.trim().length > 20);"
        " range.selectNodeContents(said || doc);"
        " const s = window.getSelection(); s.removeAllRanges(); s.addRange(range);"
        " doc.dispatchEvent(new MouseEvent('mouseup', {bubbles: true})); })()"
    )
    await page.settle(0.5)
    assert await page.evaluate("!document.querySelector('#rail-comment').hidden")
    sent = await page.evaluate(
        "(async () => { const f = document.querySelector('#rail-comment-form');"
        " f.querySelector('textarea').value = 'The evidence here is thin.';"
        " f.requestSubmit(); await new Promise((r) => setTimeout(r, 800));"
        " return document.querySelector('#rail-comment').hidden; })()"
    )
    assert sent is True, "the comment is sent and the box closes"
    assert page.console == [], page.console


async def test_the_dock_has_a_board_a_draft_and_the_day(page: Page) -> None:
    """UI-2d: beside a thread, Board, Draft and Calendar tabs.
    The Board follows the work's plan: waves of a team plan, or the lead's list."""
    await page.viewport(1440, 900)
    started = await page.evaluate(
        "api('/topics', {method: 'POST', body: {text: '@Nobody-here a dock check'}})"
        ".then((r) => r.topic.id)"
    )
    await show(page, f"topic/{started}")
    await page.settle(1.5)
    tabs = await page.evaluate(
        "[...document.querySelectorAll('#topic-rail [role=tab]')].map((t) => t.textContent)"
    )
    assert tabs == ["Board", "Draft", "Files", "Calendar"], tabs
    for tab, said in (
        ("draft", "Nothing delivered"),
        ("files", "business space"),
        ("calendar", "calendar"),
    ):
        await page.evaluate(f"document.querySelector('[data-action=\"dock-tab:{tab}\"]').click()")
        await page.settle(1.0)
        panel = await page.evaluate("document.querySelector('#dock-panel')?.innerText || ''")
        assert said.lower() in panel.lower(), (tab, panel)
        chosen = f"document.querySelector('#dock-tab-{tab}').getAttribute('aria-selected')"
        assert await page.evaluate(chosen) == "true"
    assert await page.evaluate("JSON.parse(localStorage.getItem('office:dockTab'))") == "calendar"
    await page.evaluate("document.querySelector('[data-action=\"dock-tab:board\"]').click()")
    await page.settle(0.6)
    waves = await page.evaluate(
        "(() => { roomReader.work = [{run_id: 'w1', title: 'Win-back plan', status: 'COMPLETED',"
        " todo: []}]; Dock.flowFor = 'w1'; Dock.flowAt = Date.now(); Dock.flow = {steps: ["
        " {id: 'a', level: 1, brief: 'Market size', role: 'researcher', status: 'done'},"
        " {id: 'b', level: 2, brief: 'Pricing', role: 'strategist', status: 'running'}]};"
        " return dockPlan(roomReader.work[0]); })()"
    )
    assert "Wave 1 · done" in waves and "Wave 2 · working" in waves, waves
    assert "1 · Market size" in waves and "2 · Pricing" in waves, waves
    todo = await page.evaluate(
        "dockPlan({run_id: 'w2', todo: [{text: 'Find prices', status: 'done'},"
        " {text: 'Write tiers', status: 'in_progress'}]})"
    )
    assert "Find prices" in todo and "is-working" in todo and "Wave" not in todo, todo
    await page.evaluate(
        f"api('/topics/{started}', {{method: 'PUT', body: {{status: 'archived'}}}})"
    )
    assert page.console == [], page.console


async def test_a_decision_is_recorded_and_shows_in_its_space(page: Page) -> None:
    """TF6: Record as decision on the owner's own message; the space shows it."""
    await page.viewport(1440, 900)
    await show(page, "home")
    stamp = await page.evaluate("Date.now()")
    venture = await page.evaluate(
        f"api('/ventures', {{method: 'POST', body: {{name: 'Line {stamp}'}}}}).then((v) => v.id)"
    )
    name = f"Space project {stamp}"
    project = await page.evaluate(
        f"api('/projects', {{method: 'POST', body: {{name: '{name}'}}}}).then((p) => p.id)"
    )
    try:
        await page.evaluate(
            f"api('/projects/{project}/venture',"
            f" {{method: 'PUT', body: {{venture_id: '{venture}'}}}})"
            ".catch(() => null)"
        )
        home = await page.evaluate(f"api('/projects/{project}/topic').then((t) => t.id)")
        await page.evaluate(
            f"api('/topics/{home}/messages', {{method: 'POST',"
            " body: {text: '@Nobody-here we start with a human-delivered diagnosis.'}})"
        )
        await show(page, f"topic/{home}")
        await page.settle(1.5)
        mid = await page.evaluate(
            "document.querySelector('#room-feed .room-message.mine').dataset.mid"
        )
        await page.evaluate(
            f"document.querySelector('[data-action=\"topic-decide:{mid}\"]').click()"
        )
        await page.settle(1.0)
        await page.evaluate("document.querySelector('#topic-decide-form').requestSubmit()")
        await page.settle(2.5)
        rail = await page.evaluate("document.querySelector('#topic-rail')?.innerText || ''")
        assert "human-delivered diagnosis" in rail, rail
        listed = await page.evaluate(f"api('/topics/{home}/decisions')")
        assert len(listed) == 1
        assert page.console == [], page.console
    finally:
        await page.evaluate(
            f"api('/projects/{project}', {{method: 'DELETE', body: {{confirm: '{name}'}}}})"
        )


async def test_mobile_access_shows_both_ways_in_without_claiming_a_phone(page: Page) -> None:
    """MA4: the home network and the remote route each say where they stand,
    and nothing says a phone is connected before one has signed in."""
    for width in (1440, 390):
        await page.viewport(width, 900)
        await show(page, "access")
        text = await page.evaluate("document.querySelector('#content')?.innerText || ''")
        assert "Home network" in text and "Remote access" in text, text
        assert "awake" in text, "the Mac must stay awake, and the page says so"
        data = await page.evaluate("api('/access')")
        if not any(point.get("confirmed_at") for point in data["points"]):
            assert "Confirmed from" not in text
        wide = await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        assert wide, f"no sideways scrolling at {width}px"
    await show(page, "office")
    assert "Mobile & remote access" in await page.evaluate(
        "document.querySelector('#content')?.innerText || ''"
    )
    assert page.console == [], page.console


# Controls narrower than the screen but pushed past its edge are clipped.
CLIPPED = (
    "[...document.querySelectorAll('#content button, #content a.button, #content input,"
    " #content textarea, #content select')].filter((el) => {"
    " const box = el.getBoundingClientRect();"
    " return box.width > 0 && (box.right > innerWidth + 1 || box.left < -1); })"
    ".map((el) => el.outerHTML.slice(0, 80))"
)


async def test_the_core_screens_fit_a_phone(page: Page) -> None:
    """MA4: at 390 px the desk, discussions, a discussion, work and the access
    page scroll only downwards, with every control on the screen."""
    await page.viewport(390, 844)
    screens = ["home", "discussions", "tasks", "access"]
    listed = await page.evaluate("api('/topics')")
    if listed:
        screens.append(f"topic/{listed[0]['id']}")
    for screen in screens:
        await show(page, screen)
        wide = await page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert wide <= 1, f"{screen} scrolls sideways by {wide}px"
        clipped = await page.evaluate(CLIPPED)
        assert clipped == [], f"{screen}: {clipped}"
    hidden = await page.evaluate(
        "[...document.querySelectorAll('#nav > a')].filter((a) =>"
        " a.getBoundingClientRect().right > innerWidth + 1).map((a) => a.innerText)"
    )
    assert hidden == [], f"places past the edge of a phone: {hidden}"
    assert page.console == [], page.console


async def test_every_page_below_the_top_has_a_way_back(page: Page) -> None:
    """A page opened from another goes back to it; a page opened directly (a
    link, a home-screen icon with no browser buttons) goes up to its parent.
    The trail above names each level, and each level above is a link."""
    for width in (1440, 390):
        await page.viewport(width, 900)
        await show(page, "home")
        assert await page.evaluate("!document.querySelector('#back-button')"), "no back at the top"
        await show(page, "office")
        await show(page, "access")
        links = await page.evaluate(
            "[...document.querySelectorAll('#breadcrumb a')].map((a) => a.getAttribute('href'))"
        )
        assert "#office" in links, links
        trail = await page.evaluate("document.querySelector('#breadcrumb')?.innerText || ''")
        if width > 580:
            assert "Office settings" in trail and "Mobile & remote access" in trail, trail
        else:
            # On a phone the header holds Back, the status pill and Ask.
            assert trail.strip() == "←", trail
        await page.evaluate("document.querySelector('#back-button')?.click()")
        await page.settle(1.6)
        assert await page.evaluate("location.hash") == "#office", "back returns where I came from"
    # Opened directly, with nothing behind it in the office: up to the parent.
    await page.evaluate("Office.visited = []")
    await show(page, "agents")
    await page.evaluate("Office.visited = [location.hash]")
    await page.evaluate("document.querySelector('#back-button')?.click()")
    await page.settle(1.6)
    assert await page.evaluate("location.hash") == "#office"
    assert page.console == [], page.console


async def test_a_provider_outage_says_so_and_offers_one_retry(page: Page) -> None:
    """6 Oct: research that could not run showed the critic's concerns, and
    each review choice returned to the same stop."""
    await show(page, "home")
    card = await page.evaluate(
        "attentionFor({id: 'r1', status: 'NEEDS_HUMAN_REVIEW', state: {stopped_reason:"
        " 'providers_unavailable', provider_failure: {codes: ['provider_unavailable',"
        " 'rate_limited'], steps: 4}}}, null, null, [])"
    )
    assert "The research could not run" in card
    assert "4 research steps" in card and "rate-limited" in card and "unavailable" in card
    assert 'data-action="providers-retry"' in card and 'data-action="go-providers"' in card
    assert "critic" not in card.lower(), "no critic's concerns about work that never ran"
    assert page.console == [], page.console


async def test_the_desk_shows_what_needs_you_and_the_team(page: Page) -> None:
    """UI-1c: the Desk replaces the floor plan. The sidebar
    down the left; what needs the owner, counted; the team beside it."""
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.settle(1.5)
    bar = await page.evaluate(
        "(() => { const r = document.querySelector('.shell > aside').getBoundingClientRect();"
        " return {top: r.top, height: r.height, width: r.width}; })()"
    )
    assert bar["top"] == 0 and bar["height"] >= 880 and bar["width"] < 300, bar
    assert await page.evaluate("document.querySelectorAll('.floor-room').length") == 0
    needs = await page.evaluate(
        "document.querySelector('.desk-section[aria-label=\"Needs you\"]')?.innerText || ''"
    )
    desk = await page.evaluate("api('/office/desk')")
    if desk["needs"]:
        assert str(len(desk["needs"])) in needs, needs
    else:
        assert "Nothing is waiting for you" in needs, needs
    font = await page.evaluate("getComputedStyle(document.body).fontFamily")
    assert font.startswith('"Archivo"') or font.startswith("Archivo"), font
    assert page.console == [], page.console


async def test_a_busy_colleague_shows_as_working_beside_the_desk(page: Page) -> None:
    await show(page, "home")
    team = await page.evaluate(
        "deskTeam([{id: 'a1', name: 'Davi', role: 'researcher'},"
        " {id: 'a2', name: 'Slac', role: 'critic'}],"
        " [{run_id: 'r1', title: 'Compare frameworks', step: 'research'}])"
    )
    assert 'href="#agent/a1"' in team and "Davi" in team
    rows = await page.evaluate(
        "(() => { const box = document.createElement('div'); box.innerHTML = deskTeam("
        "[{id: 'a1', name: 'Davi', role: 'researcher'}, {id: 'a2', name: 'Slac', role: 'critic'}],"
        " [{run_id: 'r1', title: 'Compare frameworks', step: 'research'}]);"
        " return [...box.querySelectorAll('.desk-colleague')].map((a) => a.innerText); })()"
    )
    assert "working" in rows[0] and "free" in rows[1], rows
    assert page.console == [], page.console


async def test_a_task_shows_how_it_moved_across_the_floor(page: Page) -> None:
    await show(page, "home")
    stops = await page.evaluate(
        """floorPath([
          {kind: 'step_started', payload: {node: 'plan'}, created_at: '2026-10-06T18:56:00Z'},
          {kind: 'step_started', payload: {node: 'research'}, created_at: '2026-10-06T18:57:00Z'},
          {kind: 'budget_ask', payload: {limit: 'cost'}, created_at: '2026-10-06T19:14:00Z'},
          {kind: 'finish_requested', payload: {}, created_at: '2026-10-06T20:07:00Z'},
          {kind: 'run_finished', payload: {status: 'COMPLETED'},
           created_at: '2026-10-06T20:09:00Z'},
        ]).map((s) => s.place)"""
    )
    assert stops == ["Chief's office", "Research bay", "Stopped at the door", "You", "Done"]
    assert page.console == [], page.console


async def test_the_calendars_page_connects_each_kind_its_own_way(page: Page) -> None:
    """CAL1: Google and Outlook by a private link; iCloud by an app password."""
    await page.viewport(1440, 900)
    await show(page, "calendars")
    await page.settle(1.5)
    kinds = await page.evaluate(
        "[...document.querySelectorAll('.cal-add strong')].map((n) => n.textContent)"
    )
    for name in ("Google Calendar", "iCloud (iPhone, iPad, Mac)", "Outlook and Microsoft 365"):
        assert name in kinds, kinds
    await page.evaluate("document.querySelector('[data-action=\"cal-add:icloud\"]').click()")
    await page.settle(0.8)
    form = await page.evaluate(
        "(() => { const f = document.querySelector('#cal-connect');"
        " return {password: !!f.querySelector('#f-cal-password[type=password]'),"
        " link: !!f.querySelector('#f-cal-url'), help: f.innerText}; })()"
    )
    assert form["password"] and not form["link"], form
    assert "app-specific password" in form["help"]
    await page.evaluate("closeModal()")
    await page.evaluate("document.querySelector('[data-action=\"cal-add:google\"]').click()")
    await page.settle(0.8)
    assert await page.evaluate("!!document.querySelector('#cal-connect #f-cal-url')")
    await page.evaluate("closeModal()")
    shared = await page.evaluate(
        "document.querySelector('#cal-preferences input[name=share]:checked')?.value"
    )
    assert shared == "titles", "the owner's choice on 7 Oct is the default"
    assert page.console == [], page.console


async def test_your_day_shows_meetings_free_time_and_the_prep_offer(page: Page) -> None:
    """CAL2/CAL3: the strip, the owner's status, and a brief offered, never started."""
    await show(page, "home")
    strip = await page.evaluate(
        """(() => {
          const at = (h, m = 0) => { const d = new Date(); d.setHours(h, m, 0, 0);
            return d.toISOString(); };
          return floorDay({connected: 1, hold: true,
            status: {state: 'busy', until: at(11)},
            prep: {id: 7, title: 'Team sync', starts_at: at(16), cost: 0.3},
            events: [
              {title: 'Client call', starts_at: at(10), ends_at: at(11), busy: true,
               all_day: false},
              {title: 'Team sync', starts_at: at(16), ends_at: at(16, 45), busy: true,
               all_day: false},
              {title: 'Day of focus', starts_at: at(0), ends_at: at(23, 59), busy: false,
               all_day: true}]});
        })()"""
    )
    assert "Your day" in strip and "Client call" in strip and "Day of focus" in strip
    assert "In a meeting until" in strip
    assert 'data-action="cal-prep-start:7:' in strip and "up to $0.30" in strip
    assert 'data-action="cal-prep-dismiss:7"' in strip
    empty = await page.evaluate("floorDay({connected: 0})")
    assert 'href="#calendars"' in empty and "Connect your calendar" in empty
    assert page.console == [], page.console


async def test_calendars_offer_the_mac_and_sign_ins_before_any_password(page: Page) -> None:
    """CAL5-7: consent first; pasting a link or password is the fallback."""
    await page.viewport(1440, 900)
    await show(page, "calendars")
    await page.settle(1.5)
    order = await page.evaluate(
        "[...document.querySelectorAll('.cal-mac strong, .cal-sign-in strong, .cal-more summary')]"
        ".map((n) => n.textContent.trim())"
    )
    has_mac = await page.evaluate("Cal.sources.some((s) => s.kind === 'mac')")
    # Once the Mac's calendars are connected, its button gives way to the sign-ins.
    expected = ["Sign in with Google", "Sign in with Microsoft"]
    if not has_mac:
        expected = ["Use the calendars on this Mac", *expected]
    assert order[: len(expected)] == expected, order
    assert await page.evaluate("!document.querySelector('.cal-more').open"), "passwords tucked away"
    await page.evaluate("document.querySelector('[data-action=\"cal-app:google\"]').click()")
    await page.settle(0.8)
    guide = await page.evaluate(
        "(() => { const f = document.querySelector('#cal-app'); return {"
        " address: f.querySelector('#cal-redirect').value,"
        " secret: !!f.querySelector('#f-client_secret'),"
        " steps: f.querySelectorAll('li').length}; })()"
    )
    assert (
        guide["address"].endswith("/calendars/oauth/callback")
        and "127.0.0.1" not in guide["address"]
    )
    assert guide["secret"] and guide["steps"] >= 4
    await page.evaluate("closeModal()")
    await page.evaluate("document.querySelector('[data-action=\"cal-app:microsoft\"]').click()")
    await page.settle(0.8)
    assert not await page.evaluate("!!document.querySelector('#cal-app #f-client_secret')"), (
        "Microsoft needs no secret"
    )
    await page.evaluate("closeModal()")
    card = await page.evaluate(
        "calSourceCard({id: 'm', kind: 'mac', provider: 'mac', helper: 'running', calendars: [],"
        " last_error: 'mac_access_denied', last_synced_at: null, event_count: 0})"
    )
    assert "System Settings" in card and "Ask again" in card
    stopped = await page.evaluate(
        "calSourceCard({id: 'm', kind: 'mac', provider: 'mac', helper: 'not_running',"
        " calendars: [], last_error: null, last_synced_at: null, event_count: 0})"
    )
    assert "scripts.launch" in stopped
    assert page.console == [], page.console


async def test_the_calendar_shows_day_week_month_and_year(page: Page) -> None:
    """CAL8: the owner asked to see their calendar by day, week, month, year."""
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.evaluate(
        """(async () => {
          await api('/calendars/mac', {method: 'POST'});
          const now = new Date(); const at = (h) => { const d = new Date(now);
            d.setHours(h, 0, 0, 0); return d.toISOString(); };
          await api('/calendars/mac/helper', {method: 'POST', body: {access: 'granted',
            calendars: [{id: 'home', name: 'Home', account: 'iCloud', writable: true,
                         color: '#e8590c'},
                        {id: 'hol', name: 'Holidays', account: 'Subscribed', writable: false,
                         color: '#2e7d32'}],
            events: [{uid: 'a@1', calendar: 'Home', calendar_id: 'home', title: 'Dentist',
                      starts_at: at(10), ends_at: at(11)},
                     {uid: 'h@1', calendar: 'Holidays', calendar_id: 'hol', title: 'Holiday',
                      starts_at: at(0), ends_at: at(23), all_day: true}]}});
        })()"""
    )
    try:
        for mode, part in (
            ("agenda", ".calv-agenda"),
            ("day", ".calv-grid"),
            ("week", ".calv-grid"),
            ("month", ".calv-month"),
            ("year", ".calv-year"),
        ):
            await show(page, f"calendar/{mode}")
            await page.settle(1.0)
            assert await page.evaluate(f"!!document.querySelector('{part}')"), mode
            pressed = await page.evaluate(
                "document.querySelector('.calv-modes [aria-pressed=true]')?.textContent"
            )
            assert pressed.lower() == mode
        await show(page, "calendar/week")
        await page.settle(1.0)
        assert "Dentist" in await text(page)
        await page.evaluate(
            "[...document.querySelectorAll('.calv-event')]"
            ".find((b) => b.textContent.includes('Dentist')).click()"
        )
        await page.settle(0.6)
        assert await page.evaluate("!!document.querySelector('[data-action^=\"calv-edit:\"]')")
        await page.evaluate("closeModal()")
        await page.evaluate(
            "[...document.querySelectorAll('.calv-chip')]"
            ".find((b) => b.textContent.includes('Holiday')).click()"
        )
        await page.settle(0.6)
        said = await page.evaluate("document.querySelector('#modal-content').innerText")
        assert "read-only" in said and "Edit" not in said.split("\n")[-1]
        await page.evaluate("closeModal()")
        await page.evaluate("document.querySelector('[data-action=\"calv-new\"]').click()")
        await page.settle(0.6)
        choices = await page.evaluate(
            "[...document.querySelectorAll('#f-calv-calendar option')].map((o) => o.value)"
        )
        assert choices == ["home"], "only calendars macOS lets the owner change"
        await page.evaluate("closeModal()")
        assert page.console == [], page.console
    finally:
        await page.evaluate(
            "api('/calendars').then((d) => Promise.all(d.sources.map((s) =>"
            " api(`/calendars/${s.id}`, {method: 'DELETE'}))))"
        )


async def test_overlapping_events_share_width_only_with_each_other(page: Page) -> None:
    """10 Oct: one overlap no longer narrows every event of the day; an event
    widens into lanes its neighbours do not use."""
    await show(page, "home")
    placed = await page.evaluate(
        """calLanes([
          {id: 'a', start: 9, end: 10}, {id: 'b', start: 9.75, end: 10.5},
          {id: 'c', start: 11, end: 12},
          {id: 'd', start: 13, end: 17}, {id: 'e', start: 13, end: 14},
          {id: 'f', start: 13.5, end: 14.5}, {id: 'g', start: 15, end: 16}
        ]).map((i) => [i.id, i.lane, i.lanes, i.span])"""
    )
    by = {row[0]: row[1:] for row in placed}
    assert by["a"] == [0, 2, 1] and by["b"] == [1, 2, 1], "two overlapping share"
    assert by["c"] == [0, 1, 1], "alone, it keeps the whole width"
    assert by["d"][1] == 3 and by["g"] == [1, 3, 2], "later, it widens into free lanes"
    rows = await page.evaluate(
        """(() => {
          const at = (h) => { const d = new Date(); d.setHours(h, 0, 0, 0);
            return d.toISOString(); };
          const html = floorDay({connected: 1, events: [
            {title: 'Workshop', starts_at: at(9), ends_at: at(16), busy: true, all_day: false},
            {title: 'Lunch', starts_at: at(12), ends_at: at(13), busy: true, all_day: false}]});
          const box = document.createElement('div'); box.innerHTML = html;
          return [...box.querySelectorAll('.floor-day-event')].map((e) => e.style.top);
        })()"""
    )
    assert len(set(rows)) == 2, "on the floor, an overlapping event gets a row of its own"
    assert await page.evaluate("calSpan('2026-10-10T09:00:00', '2026-10-10T10:00:00')") in (
        "9–10am",
        "09:00–10:00",
    )


async def test_a_calendar_proposal_shows_its_time_and_one_button(page: Page) -> None:
    """CAL4: an agent proposes; the owner's press opens the filled form."""
    await show(page, "home")
    offered = await page.evaluate(
        "roomMessageActions({id: 'm:5', actions: [{kind: 'add_event', title: 'Draft',"
        " starts_at: '2026-10-08T08:00:00Z', ends_at: '2026-10-08T10:00:00Z'}]})"
    )
    assert "Proposed for your calendar" in offered and "Draft" in offered
    assert 'data-action="room-calendar:5:0"' in offered
    added = await page.evaluate(
        "roomMessageActions({id: 'm:5', actions: [{kind: 'add_event', title: 'Draft',"
        " starts_at: '2026-10-08T08:00:00Z', ends_at: '2026-10-08T10:00:00Z', change_id: 9}]})"
    )
    assert "Added" in added and "room-calendar" not in added
    form = await page.evaluate(
        """(() => { CalView.writable = [{id: 'home', name: 'Home', account: 'iCloud'}];
          calEditForm(null, new Date('2026-10-08T08:00:00Z'),
                      {title: 'Draft', ends_at: '2026-10-08T10:00:00Z'});
          const f = document.querySelector('#calv-form');
          const out = {title: f.querySelector('#f-calv-title').value,
                       ends: f.querySelector('#f-calv-end-time').value};
          closeModal(); return out; })()"""
    )
    assert form["title"] == "Draft" and form["ends"], form
    assert page.console == [], page.console


async def test_the_office_browser_shows_its_steps_on_the_task(page: Page) -> None:
    """CU3: each step the browser took, with the screenshot it took then."""
    await show(page, "home")
    card = await page.evaluate(
        """browserCard({id: 'r1', status: 'RUNNING'}, [
          {kind: 'browser_step', payload: {step: 1, action: 'goto', said: 'goto https://example.com',
            title: 'Example Domain', url: 'https://example.com/', shot: '1.jpg'}},
          {kind: 'browser_step', payload: {step: 2, action: 'click', said: 'click 1',
            title: 'IANA', url: 'https://www.iana.org/', shot: '2.jpg'}},
          {kind: 'step_completed', payload: {node: 'work'}}])"""
    )
    assert "The office browser" in card and "working" in card
    assert 'src="/runs/r1/browser/2.jpg"' in card, "the latest page is shown large"
    assert 'data-action="browser-shot:1.jpg"' in card and "Example Domain" in card
    assert await page.evaluate("browserCard({id: 'r1', status: 'COMPLETED'}, [])") == ""
    switch = await page.evaluate(
        "browserSwitchCard({settings: {browser_on: true}, browser_running: false})"
    )
    assert "not running" in switch and 'data-action="browser-switch:off"' in switch
    off = await page.evaluate("browserSwitchCard({settings: {browser_on: false}})")
    assert 'data-action="browser-switch:on"' in off and "asks you first" in off
    assert page.console == [], page.console


async def press(page: Page, action: str) -> None:
    """Click the button that carries this data-action."""
    await page.evaluate(
        f"document.querySelector('[data-action=' + {json.dumps(json.dumps(action))} + ']').click()"
    )


async def test_files_and_folders_can_be_attached_where_work_starts(page: Page) -> None:
    """AT1: the owner could not give a file or folder when starting work."""
    await page.viewport(1440, 900)
    await show(page, "home")
    await page.settle(1.0)
    menu = await page.evaluate(
        "[...document.querySelectorAll('#desk-start .composer-tools button')]"
        ".map((b) => b.textContent.trim())"
    )
    assert menu == ["Files", "Folder", "Documents"], menu
    name = f"Notes {await page.evaluate('Date.now()')}"
    source = await page.evaluate(
        "(async (name) => { const s = await api('/sources/upload',"
        " {method: 'POST', body: {name}});"
        " await api(`/sources/${s.id}/documents`, {method: 'POST',"
        " body: {path: 'n.md', text: 'Hello'}}); return s.id; })(" + json.dumps(name) + ")"
    )
    try:
        await press(page, "attach-existing:desk-start")
        await page.settle(1.0)
        await press(page, f"attach-choose:desk-start:{source}")
        await page.settle(0.4)
        chips = await page.evaluate("document.querySelector('#desk-start-chips').innerText")
        assert name in chips and "1 document" in chips
        assert await page.evaluate("attachedIds('desk-start')") == [source]
        await press(page, f"attach-remove:desk-start:{source}")
        assert await page.evaluate("attachedIds('desk-start')") == []
        await page.evaluate("openNewTask()")
        await page.settle(1.2)
        assert await page.evaluate("!!document.querySelector('#new-task-form .attach-row')")
        await page.evaluate("closeModal()")
        assert page.console == [], page.console
    finally:
        await page.evaluate(f"api('/sources/{source}', {{method: 'DELETE'}})")


async def test_an_open_thread_hears_a_new_message_without_asking(page: Page) -> None:
    """Stage 5b: the office's stream redraws the thread on screen as soon as
    something is said in it, from anywhere; the timers are only a fallback."""
    await page.viewport(1440, 900)
    started = await page.evaluate(
        "api('/topics', {method: 'POST', body: {text: '@Nobody-here a live check'}})"
        ".then((r) => r.topic.id)"
    )
    await show(page, f"topic/{started}")
    await page.settle(2.0)
    assert await page.evaluate("liveConnected()") is True, "the stream is open"
    words = f"Said elsewhere {uuid4().hex[:6]}"
    # Hold the fallback back, so only the stream can bring it.
    await page.evaluate("Live.timers['polled:thread'] = Date.now()")
    await page.evaluate(
        f"fetch('/topics/{started}/messages', {{method: 'POST', credentials: 'same-origin',"
        " headers: {'Content-Type': 'application/json', 'X-CSRF-Token': Office.csrf},"
        f" body: JSON.stringify({{text: {json.dumps(words)}}})}})"
    )
    await page.settle(1.5)
    feed = await page.evaluate("document.querySelector('#room-messages')?.innerText || ''")
    assert words in feed, feed[-400:]
    await page.evaluate(
        f"api('/topics/{started}', {{method: 'PUT', body: {{status: 'archived'}}}})"
    )
    assert page.console == [], page.console


async def test_a_task_shows_its_model_calls(page: Page) -> None:
    """Stage 5e: a task's page lists the calls its models were sent."""
    await page.viewport(1440, 900)
    tasks = await page.evaluate("api('/office/overview').then((o) => o.tasks.map((t) => t.run_id))")
    if not tasks:
        pytest.skip("no task here to open")
    await show(page, f"task/{tasks[0]}")
    await page.settle(2.0)
    await page.evaluate("document.querySelector('[data-action=\"tab:calls\"]').click()")
    await page.settle(2.0)
    shown = await page.evaluate("document.querySelector('#model-calls')?.innerText || ''")
    assert "No model calls kept" in shown or "Every call this task made" in shown, shown
    assert page.console == [], page.console


async def test_a_spaces_folders_are_seen_and_detached_in_settings(page: Page) -> None:
    """Stage 6c: Settings > Workspaces lists each space's folders; detaching works."""
    await page.viewport(1440, 900)
    await show(page, "home")
    name = f"Folders {uuid4().hex[:6]}"
    made = f"{{method: 'POST', body: {{name: {json.dumps(name)}}}}}"
    space = await page.evaluate(f"api('/ventures', {made}).then((v) => v.id)")
    attach = "{method: 'POST', body: {path: 'clients/acme/site'}}"
    folder = await page.evaluate(f"api('/ventures/{space}/workspaces', {attach}).then((w) => w.id)")
    await page.evaluate("settingsWindowShow('workspaces')")
    await page.settle(2.0)
    shown = await page.evaluate("document.querySelector('#settings-body')?.innerText || ''")
    assert "Your folders, given to a space." not in shown, "the page title is the section's"
    assert name in shown and "~/dev/clients/acme/site" in shown, shown[:600]
    await page.evaluate("window.confirm = () => true")
    await page.evaluate(f"document.querySelector('[data-action=\"ws-detach:{folder}\"]').click()")
    await page.settle(1.5)
    left = await page.evaluate(
        f"api('/ventures/{space}/workspaces').then((w) => w.workspaces.length)"
    )
    assert left == 0
    await page.evaluate("document.getElementById('settings-window').close()")
    assert page.console == [], page.console
