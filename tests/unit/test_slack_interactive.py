"""Slack buttons arriving over Socket Mode: read as requests, acknowledged first."""

import json

from app.channels import slack_format
from app.channels.socket_mode import process_frame, request_from_action


def click(action: str, value: str = "", thread: str | None = "111.1") -> dict:
    message = {"ts": "222.2", "text": "Shall I start this?"}
    if thread:
        message["thread_ts"] = thread
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "D1"},
        "container": {"message_ts": "222.2", "channel_id": "D1"},
        "message": message,
        "actions": [{"action_id": action, "value": value, "action_ts": "333.3"}],
    }


def test_a_start_click_is_a_request_in_the_proposals_thread() -> None:
    request = request_from_action(click(slack_format.START))
    assert request is not None
    assert (request.workspace_id, request.user_id, request.channel_id) == ("T1", "U1", "D1")
    assert request.action == slack_format.START
    assert request.thread_ts == "111.1"
    assert request.message_ts == "222.2"
    assert request.message_text == "Shall I start this?"
    assert request.event_id == "action:333.3:U1"


def test_a_click_on_a_message_outside_a_thread_uses_the_message_itself() -> None:
    request = request_from_action(click(slack_format.STOP, "run-1", thread=None))
    assert request is not None
    assert request.thread_ts == "222.2"
    assert request.value == "run-1"


def test_link_buttons_and_unknown_actions_are_not_requests() -> None:
    assert request_from_action(click(slack_format.OPEN)) is None
    assert request_from_action(click("something_else")) is None
    assert request_from_action({"type": "view_submission"}) is None


async def test_a_click_is_acknowledged_before_it_is_handled() -> None:
    order: list[str] = []

    async def ack(text: str) -> None:
        order.append("ack")

    async def handle(request) -> None:
        order.append(f"handle:{request.action}")

    frame = json.dumps(
        {"type": "interactive", "envelope_id": "e1", "payload": click(slack_format.NOT_NOW)}
    )
    assert await process_frame(frame, "B1", ack, handle) == "handled"
    assert order == ["ack", f"handle:{slack_format.NOT_NOW}"]
