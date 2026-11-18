"""Quick answers that look things up, from Slack, on fakes: nothing is fetched."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.channels import slack_format
from app.channels.base import ChannelError
from app.channels.inbound import InboundHandler, InboundRequest, Pairing
from app.channels.lookup import (
    MAX_CALLS,
    ChatAnswer,
    material,
    parse_plan,
    picture_parts,
    run_calls,
    web_address,
)
from app.channels.slack import SlackAdapter
from app.channels.socket_mode import request_from_event


def test_the_plan_is_read_and_anything_else_means_answer() -> None:
    assert parse_plan('{"do": "answer"}') == {"do": "answer"}
    looked = parse_plan(
        'Sure. {"do": "lookup", "calls": [{"tool": "look", "input": "https://www.buienradar.nl/"},'
        ' {"tool": "run", "input": "rm -rf /"}]}'
    )
    assert looked == {
        "do": "lookup",
        "calls": [{"tool": "look", "input": "https://www.buienradar.nl/"}],
    }
    assert parse_plan('{"do": "task", "objective": "Compare three CRMs"}') == {
        "do": "task",
        "objective": "Compare three CRMs",
    }
    assert parse_plan("no json here") == {"do": "answer"}
    assert parse_plan('{"do": "lookup", "calls": []}') == {"do": "answer"}


def test_only_public_web_addresses_are_opened() -> None:
    assert web_address("https://www.buienradar.nl/weer/leiden") is not None
    for refused in (
        "file:///etc/passwd",
        "http://localhost:8000/",
        "http://192.168.1.4/",
        "leiden",
    ):
        assert web_address(refused) is None, refused


async def test_lookups_are_capped_and_one_failure_does_not_spoil_the_rest() -> None:
    seen: list[str] = []

    async def search(query: str) -> list[Any]:
        seen.append(f"search:{query}")
        return [
            SimpleNamespace(title="KNMI", url="https://knmi.nl", snippet="Rain at 15:00", text="")
        ]

    async def read(address: str) -> Any:
        raise RuntimeError("unreachable")

    async def look(address: str) -> tuple[str, bytes | None]:
        seen.append(f"look:{address}")
        return "Radar Lyon", b"\xff\xd8jpeg"

    calls = [
        {"tool": "search", "input": "rain Lyon 15:00"},
        {"tool": "read", "input": "https://knmi.nl/"},
        {"tool": "look", "input": "https://www.buienradar.nl/"},
        {"tool": "look", "input": "https://example.org/"},
    ]
    found = await run_calls(calls, search, read, look)
    assert len(found) == MAX_CALLS
    assert found[0].text.startswith("- KNMI") and found[1].error and found[2].picture
    parts = material(found, pictures=True)
    assert "Rain at 15:00" in parts[0]["text"] and "failed" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert len(material(found, pictures=False)) == 1, "a model that cannot see gets words only"


async def test_a_tool_the_office_does_not_run_is_said_not_crashed() -> None:
    [item] = await run_calls([{"tool": "look", "input": "https://x.org/"}], None, None, None)
    assert item.error == "browser_not_running"


async def test_an_address_the_office_may_not_open_never_reaches_the_reader() -> None:
    opened: list[str] = []

    async def read(address: str) -> Any:
        opened.append(address)
        return SimpleNamespace(text="secret")

    [item] = await run_calls(
        [{"tool": "read", "input": "http://localhost/admin"}], None, read, None
    )

    assert opened == []
    assert item.error == "address_not_allowed"


async def test_a_failed_read_still_names_the_page_it_tried() -> None:
    async def read(address: str) -> Any:
        raise RuntimeError("timed out")

    found = await run_calls([{"tool": "read", "input": "https://knmi.nl/"}], None, read, None)

    assert "[read https://knmi.nl/] failed: timed out" in material(found, pictures=False)[0]["text"]


def test_shared_pictures_become_image_parts() -> None:
    [part] = picture_parts([("radar.png", b"\x89PNG")])
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_message_with_a_screenshot_is_a_request_with_its_file() -> None:
    payload = {
        "team_id": "T1",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "subtype": "file_share",
            "channel_type": "im",
            "user": "U1",
            "channel": "D1",
            "ts": "1.1",
            "text": "Here is the radar",
            "files": [
                {
                    "id": "F1",
                    "name": "radar.png",
                    "mimetype": "image/png",
                    "size": 2048,
                    "url_private_download": "https://files.slack.com/files-pri/T1-F1/download/radar.png",
                }
            ],
        },
    }
    request = request_from_event(payload, "UBOT")
    assert request is not None and request.text == "Here is the radar"
    assert request.files[0]["name"] == "radar.png" and request.files[0]["mimetype"] == "image/png"
    edited = {**payload, "event": {**payload["event"], "subtype": "message_changed"}}
    assert request_from_event(edited, "UBOT") is None


async def test_files_are_fetched_only_from_slack_and_only_with_permission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "noscope" in str(request.url):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>sign in")
        if "big" in str(request.url):
            return httpx.Response(200, content=b"x" * 50)
        return httpx.Response(200, content=b"\x89PNG data")

    slack = SlackAdapter(transport=httpx.MockTransport(handler))
    assert await slack.download("xoxb", "https://files.slack.com/f/ok.png", 1000) == b"\x89PNG data"
    with pytest.raises(ChannelError, match="file_address_not_slack"):
        await slack.download("xoxb", "https://evil.example/f.png", 1000)
    with pytest.raises(ChannelError, match="missing_scope"):
        await slack.download("xoxb", "https://files.slack.com/f/noscope.png", 1000)
    with pytest.raises(ChannelError, match="file_too_large"):
        await slack.download("xoxb", "https://files.slack.com/f/big.png", 10)


class Office:
    """Just enough office for the handler: an owner, a router, and a chat that answers."""

    def __init__(self, said: Any) -> None:
        self.said = said
        self.recent: list[Any] = []
        self.shared: list[Any] = []
        self.conversed: list[Any] = []

    def owner_of(self, workspace: str) -> str:
        return "U1"

    def role_of(self, workspace: str, user: str) -> str | None:
        return "owner" if user == "U1" else None

    def first_time(self, event_id: str) -> bool:
        return True

    async def route(self, text: str, recent: list | None = None) -> dict[str, tuple[str, float]]:
        self.recent.append(recent)
        return {"action": ("lookup", 0.9), "project": ("none", 0.1)}

    def active_task(self, channel: str, thread: str | None) -> None:
        return None

    def addressed_agent(self, text: str) -> None:
        return None

    async def converse(
        self,
        history: list,
        text: str,
        agent: Any = None,
        files: Any = (),
        chat_ref: Any = None,
        **kept: Any,
    ) -> Any:
        self.conversed.append((text, files))
        return self.said

    async def share(self, channel: str, thread: str | None, files: list) -> None:
        self.shared.append((channel, thread, files))

    def thread_project(self, channel: str, thread: str | None) -> None:
        return None

    def project_id(self, name: str) -> None:
        return None

    def work_offer(self, project: str) -> str:
        return f"I can start this in {project}:"


class Replies:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def __call__(
        self, channel: str, thread: Any, text: str, persona: Any = None, blocks: Any = None
    ) -> None:
        self.sent.append((text, blocks))


def asked(text: str, files: tuple = (), event: str = "e1") -> InboundRequest:
    return InboundRequest("T1", "U1", "D1", text, event, "t1", files=files)


async def test_a_looked_up_answer_brings_its_picture() -> None:
    office = Office(ChatAnswer(text="Dry until 16:00.", files=[("looked-at-1.jpg", b"jpg")]))
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Will it rain in Lyon at 15:00?"))
    assert replies.sent[0][0] == "Dry until 16:00."
    assert office.shared == [("D1", "t1", [("looked-at-1.jpg", b"jpg")])]


async def test_work_the_chat_cannot_do_inline_is_offered_with_start() -> None:
    office = Office(ChatAnswer(text="", task="Compare three CRMs for a 10-person team"))
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Which CRM should we use?"))
    text, blocks = replies.sent[0]
    assert "Compare three CRMs" in text and "Press Start" in text
    assert blocks[-1]["elements"][0]["action_id"] == slack_format.START


async def test_the_router_reads_the_thread_and_files_reach_the_chat() -> None:
    office = Office("Thanks, I see the radar.")
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Can you check Buienradar?"))
    shot = ({"name": "radar.png", "mimetype": "image/png", "size": 10, "url": "u"},)
    await handler.handle(asked("I shared you the screenshot here", shot, event="e2"))
    assert (
        office.recent[0] is None and office.recent[1][0]["content"] == "Can you check Buienradar?"
    )
    assert office.conversed[1] == ("I shared you the screenshot here", shot)
    assert all("Press Start" not in text for text, _ in replies.sent), "a remark is not new work"


def test_a_watch_plan_is_read_and_checked_for_shape() -> None:
    """QA5: "tell me if it rains before 15:00" is a watch to offer, not an answer."""
    planned = parse_plan(
        '{"do": "watch", "kind": "page", "target": "https://www.buienradar.nl/weer/leiden",'
        ' "condition": "rain in Lyon before 15:00", "every_minutes": 60,'
        ' "title": "Rain in Lyon"}'
    )
    assert planned == {
        "do": "watch",
        "watch": {
            "kind": "page",
            "target": "https://www.buienradar.nl/weer/leiden",
            "condition": "rain in Lyon before 15:00",
            "every_minutes": 60,
            "title": "Rain in Lyon",
        },
    }
    odd_interval = parse_plan(
        '{"do": "watch", "kind": "search", "target": "kettle prices", "every_minutes": 5}'
    )
    assert odd_interval["watch"]["every_minutes"] == 240, "an interval it does not know"
    assert odd_interval["watch"]["title"] == "kettle prices"
    assert parse_plan('{"do": "watch", "kind": "shell", "target": "x"}') == {"do": "answer"}
    assert parse_plan('{"do": "watch", "kind": "page", "target": " "}') == {"do": "answer"}


class WatchingOffice(Office):
    """The office, with somewhere to start a watch."""

    def __init__(self, said: Any, refuse: str | None = None) -> None:
        super().__init__(said)
        self.watched: list[Any] = []
        self.refuse = refuse

    def start_watch(self, watch: dict, channel: str, thread: str | None) -> str:
        if self.refuse:
            from app.watches import WatchRejected

            raise WatchRejected(self.refuse)
        self.watched.append((watch, channel, thread))
        return f"Watching {watch['title']}."


RAIN = {
    "kind": "page",
    "target": "https://www.buienradar.nl/weer/leiden",
    "condition": "rain in Lyon before 15:00",
    "every_minutes": 60,
    "title": "Rain in Lyon",
}


async def test_a_watch_is_offered_and_starts_only_on_the_press() -> None:
    office = WatchingOffice(ChatAnswer(text="", watch=RAIN))
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Tell me if it will rain in Lyon before 15:00"))
    text, blocks = replies.sent[0]
    assert "Rain in Lyon" in text and "every hour" in text and "$0.05" in text
    assert [b["action_id"] for b in blocks[-1]["elements"]] == [
        slack_format.START,
        slack_format.NOT_NOW,
    ]
    assert office.watched == [], "nothing runs before the press"
    pressed = InboundRequest(
        "T1", "U1", "D1", "", "e2", "t1", action=slack_format.START, message_ts="m1"
    )
    office.edit = _no_edit  # type: ignore[attr-defined]
    await handler.handle(pressed)
    assert office.watched == [(RAIN, "D1", "t1")]
    assert replies.sent[-1][0] == "Watching Rain in Lyon."


async def _no_edit(channel: str, ts: str, text: str) -> None:
    return None


async def test_a_watch_not_wanted_is_never_started() -> None:
    office = WatchingOffice(ChatAnswer(text="", watch=RAIN))
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Tell me if it will rain in Lyon before 15:00"))
    await handler.handle(asked("not now", event="e2"))
    await handler.handle(asked("yes", event="e3"))
    assert office.watched == []


async def test_a_watch_the_office_refuses_is_said_plainly() -> None:
    office = WatchingOffice(ChatAnswer(text="", watch=RAIN), refuse="address_not_allowed")
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Tell me if it will rain in Lyon before 15:00"))
    await handler.handle(asked("yes", event="e2"))
    assert "cannot watch that address" in replies.sent[-1][0]


async def test_only_the_owner_is_offered_a_watch() -> None:
    office = WatchingOffice(ChatAnswer(text="", watch=RAIN))
    office.role_of = lambda workspace, user: "member"  # type: ignore[method-assign]
    replies = Replies()
    handler = InboundHandler(office, replies, Pairing())  # type: ignore[arg-type]
    await handler.handle(asked("Tell me if it will rain in Lyon before 15:00"))
    text, blocks = replies.sent[0]
    assert "owner" in text and blocks is None
    await handler.handle(asked("yes", event="e2"))
    assert office.watched == []
