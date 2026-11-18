"""The conversation follows assistant-ui's design, in a real browser (Stage 29)."""

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


async def open_thread(page: Page, office: dict[str, Any], width: int = 1440) -> None:
    await page.viewport(width, 900)
    await page.evaluate(f"location.hash = 'topic/{office['topic']}'")
    await page.settle(3.0)


async def test_a_centred_column_with_plain_replies_and_a_round_send(
    page: Page, office: dict[str, Any]
) -> None:
    await open_thread(page, office)
    look = json.loads(
        await page.evaluate(
            """JSON.stringify((() => {
              const send = document.querySelector('#room-send');
              const reply = document.querySelector('#room-feed .room-message.agent .room-bubble');
              return {
                column: document.querySelector('#room-messages').getBoundingClientRect().width,
                rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
                replyBackground: getComputedStyle(reply).backgroundColor,
                sendWidth: send.getBoundingClientRect().width,
                sendHeight: send.getBoundingClientRect().height,
                sendRadius: getComputedStyle(send).borderRadius,
                sendName: send.getAttribute('aria-label'),
                sendWords: send.innerText,
              };
            })())"""
        )
    )
    assert look["column"] <= 44 * look["rem"] + 1, look
    assert look["replyBackground"] == "rgba(0, 0, 0, 0)", look
    assert look["sendWidth"] == look["sendHeight"] == 32, look
    assert look["sendRadius"] == "50%", look
    assert look["sendName"] == "Send" and look["sendWords"] == "Send", look
    assert page.console == []


async def test_the_action_bar_is_named_icons_quiet_until_the_last_message(
    page: Page, office: dict[str, Any]
) -> None:
    await open_thread(page, office)
    bars = json.loads(
        await page.evaluate(
            """JSON.stringify([...document.querySelectorAll('#room-feed .room-tools')]
              .map((bar) => ({
              opacity: getComputedStyle(bar).opacity,
              names: [...bar.querySelectorAll('.icon-tool')]
                .map((b) => b.getAttribute('aria-label')),
              icons: bar.querySelectorAll('.icon-tool svg').length,
            })))"""
        )
    )
    assert len(bars) >= 2, bars
    assert bars[-1]["opacity"] == "1", bars
    assert bars[0]["opacity"] == "0", bars
    for bar in bars:
        assert {"Copy", "Reply", "Reply in thread", "Branch to a new topic"} <= set(bar["names"])
        assert bar["icons"] == len(bar["names"]), bar


async def test_the_way_back_down_shows_when_the_foot_is_out_of_sight(
    page: Page, office: dict[str, Any]
) -> None:
    await open_thread(page, office)
    await page.viewport(1440, 500)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.settle(0.3)
    assert await page.evaluate("document.querySelector('#room-to-bottom').hidden") is False
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.settle(0.3)
    assert await page.evaluate("document.querySelector('#room-to-bottom').hidden") is True


async def test_only_a_new_message_gets_the_arrival_motion(
    page: Page, office: dict[str, Any]
) -> None:
    await open_thread(page, office)
    moved = await page.evaluate(
        "roomDraw(); document.querySelectorAll('#room-feed .room-arrives').length"
    )
    assert moved == 0
    moved = await page.evaluate(
        "RoomState.drawn.delete(document.querySelector('#room-feed [data-mid]').dataset.mid);"
        " roomDraw(); document.querySelectorAll('#room-feed .room-arrives').length"
    )
    assert moved == 1


async def test_a_phone_keeps_every_control_on_the_screen(
    page: Page, office: dict[str, Any]
) -> None:
    await open_thread(page, office, width=390)
    fits = json.loads(
        await page.evaluate(
            """JSON.stringify((() => {
              const send = document.querySelector('#room-send').getBoundingClientRect();
              const row = document.querySelector('#room-form .room-composer-row');
              return {
                page: document.documentElement.scrollWidth,
                sendRight: send.right,
                row: [row.scrollWidth, row.clientWidth],
              };
            })())"""
        )
    )
    assert fits["page"] <= 390, fits
    assert fits["sendRight"] <= 390, fits
    assert fits["row"][0] <= fits["row"][1], fits
    assert page.console == []


DRAW_STEPS = """(() => {
  roomReader.items.push({
    id: 'm:990001', kind: 'message', text: 'Here is what I found.', at: new Date().toISOString(),
    author: {kind: 'agent', name: 'Iris'},
    actions: [
      {kind: 'steps', steps: [
        {tool: 'search', status: 'ok'}, {tool: 'search', status: 'ok'},
        {tool: 'read', status: 'ok', source: 'https://www.example.nl/a'},
        {tool: 'look', status: 'failed', reason: '<b>page_timeout</b>'},
      ]},
      {kind: 'sources', addresses: ['https://www.example.nl/a', 'javascript:alert(1)',
                                    'local://ws/plan.md'], conversations: ['archive:abc']},
    ],
  });
  roomReader.replying = [{name: 'Tim', steps: [
    {tool: 'search_memory', status: 'ok'}, {tool: 'read', status: 'running'},
  ]}];
  roomDraw();
  const feed = document.querySelector('#room-feed');
  const said = feed.querySelector('[data-mid="m:990001"]') || feed;
  return JSON.stringify({
    kept: [...said.querySelectorAll('.room-step')].map((s) => s.textContent.trim()),
    bad: said.querySelectorAll('.room-step.bad').length,
    tag: said.querySelectorAll('.room-step b b').length,
    links: [...said.querySelectorAll('a.room-source')].map((a) => a.getAttribute('href')),
    texts: [...said.querySelectorAll('.room-sources .room-source')].map((s) => s.textContent),
    history: said.querySelector('button.room-source')?.dataset.action,
    live: [...feed.querySelectorAll('.room-typing .room-step')].map((s) => s.textContent.trim()),
    running: feed.querySelectorAll('.room-typing .room-step.running .side-spin').length,
    page: document.documentElement.scrollWidth,
  });
})()"""


@pytest.mark.parametrize("width", [1440, 390])
async def test_a_reply_shows_its_steps_and_its_sources(
    page: Page, office: dict[str, Any], width: int
) -> None:
    await open_thread(page, office, width=width)
    drawn = json.loads(await page.evaluate(DRAW_STEPS))

    assert drawn["kept"][0] == "Searched the web ×2"
    assert drawn["kept"][1] == "Read example.nl"
    assert "did not work" in drawn["kept"][2] and "page_timeout" in drawn["kept"][2]
    assert drawn["bad"] == 1 and drawn["tag"] == 0, "a reason is text, never markup"
    assert drawn["links"] == ["https://www.example.nl/a"], "only a web address is a link"
    assert "plan.md" in drawn["texts"]
    assert drawn["history"] == "archive-open:archive:abc"
    assert drawn["live"] == ["Searched your AI history", "Reading a page…"]
    assert drawn["running"] == 1
    assert drawn["page"] <= width
    assert page.console == []
