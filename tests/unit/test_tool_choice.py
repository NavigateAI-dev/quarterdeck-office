"""The one way a model's tool choice is offered and read.

Tasks (`agents/tool_round`) and discussions (`room_tools`) offer the owner's
granted tools the same way; only the purpose and the approval note differ.
"""

import json

from app.tools.choice import listing, parse_calls

OFFERED = [
    {"server": "Boards", "name": "board_list", "description": "List boards", "schema": {}},
    {
        "server": "Boards",
        "name": "board_create",
        "needs_approval": True,
        "schema": {"type": "object"},
    },
    {"server": "Paid", "name": "lookup", "price_usd": 0.5},
]


def calls(*items: object) -> str:
    return json.dumps({"calls": list(items)})


def test_listing_names_server_tool_description_and_schema() -> None:
    text = listing(OFFERED[:1], approval_note=" (asks)")

    assert text == ("- server 'Boards', tool 'board_list': List boards\n  arguments schema: {}")


def test_listing_marks_approval_and_price() -> None:
    text = listing(OFFERED, approval_note=" (asks)")

    assert "'board_create': no description (asks)\n" in text
    assert '  arguments schema: {"type":"object"}' in text
    assert "'lookup': no description (paid: up to $0.50 per call)" in text


def test_only_offered_tools_are_taken() -> None:
    text = calls(
        {"server": "Boards", "tool": "board_list", "arguments": {"q": 1}},
        {"server": "Boards", "tool": "board_delete", "arguments": {}},
        {"server": "Elsewhere", "tool": "board_list"},
        "nonsense",
    )

    assert parse_calls(text, OFFERED, limit=5) == [("Boards", "board_list", {"q": 1})]


def test_a_tool_not_offered_does_not_use_up_the_limit() -> None:
    text = calls(
        {"server": "Boards", "tool": "board_delete"},
        {"server": "Boards", "tool": "board_list"},
    )

    assert parse_calls(text, OFFERED, limit=1) == [("Boards", "board_list", {})]


def test_held_to_the_limit() -> None:
    text = calls(*[{"server": "Boards", "tool": "board_list"}] * 9)

    assert len(parse_calls(text, OFFERED, limit=3)) == 3


def test_arguments_that_are_not_an_object_become_empty() -> None:
    text = calls({"server": "Boards", "tool": "board_list", "arguments": ["x"]})

    assert parse_calls(text, OFFERED, limit=3) == [("Boards", "board_list", {})]


def test_a_fenced_choice_is_read() -> None:
    text = "```json\n" + calls({"server": "Boards", "tool": "board_list"}) + "\n```"

    assert parse_calls(text, OFFERED, limit=3) == [("Boards", "board_list", {})]


def test_an_unreadable_choice_is_no_calls() -> None:
    assert parse_calls("I would call board_list", OFFERED, limit=3) == []
    assert parse_calls('["board_list"]', OFFERED, limit=3) == []
