"""Tools in discussions, as the lead's tools: what runs, is proposed, refused or failed."""

from typing import Any

import pytest

from app import room_tools
from app.mcp.calling import ToolRefused
from app.mcp.client import McpError
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk

OFFERED = [
    {"server": "Boards", "name": "board_list", "needs_approval": False},
    {"server": "Boards", "name": "board_create", "needs_approval": True},
]


class Tools:
    def __init__(self, behaviour: dict[str, Any]) -> None:
        self.behaviour = behaviour
        self.sent: list[str] = []

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        outcome = self.behaviour[tool]
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append(tool)
        return str(outcome)


async def test_as_lead_tools_an_allowed_tool_runs_and_is_named_on_the_reply() -> None:
    from app.mcp.specs import mcp_name
    from app.tools.pipeline import ToolPipeline

    tools = Tools({"board_list": "boards", "board_create": "made"})
    used: list[dict[str, Any]] = []
    proposed: list[dict[str, Any]] = []
    pipeline = ToolPipeline(room_tools.room_specs(tools, OFFERED, used, proposed))  # type: ignore[arg-type]

    outcome = await pipeline.call(mcp_name("Boards", "board_list"), {"q": 1})

    assert outcome.text == "boards"
    assert used == [{"server": "Boards", "tool": "board_list", "outcome": "ok"}]
    assert proposed == []


async def test_as_lead_tools_one_that_asks_first_becomes_a_button_never_sent() -> None:
    from app.mcp.specs import mcp_name
    from app.tools.pipeline import ToolPipeline

    tools = Tools({"board_list": "boards", "board_create": "made"})
    proposed: list[dict[str, Any]] = []
    pipeline = ToolPipeline(room_tools.room_specs(tools, OFFERED, [], proposed))  # type: ignore[arg-type]

    outcome = await pipeline.call(mcp_name("Boards", "board_create"), {"t": "Q4"})

    assert tools.sent == []
    assert "button" in outcome.text
    assert proposed == [
        {"kind": "tool_call", "server": "Boards", "tool": "board_create", "arguments": {"t": "Q4"}}
    ]


async def test_as_lead_tools_an_office_rule_that_asks_becomes_a_button() -> None:
    from app.mcp.specs import mcp_name
    from app.tools.pipeline import ToolPipeline

    tools = Tools({"board_list": PolicyAsk(Decision(Verdict.ASK), None)})
    proposed: list[dict[str, Any]] = []
    pipeline = ToolPipeline(room_tools.room_specs(tools, OFFERED, [], proposed))  # type: ignore[arg-type]

    outcome = await pipeline.call(mcp_name("Boards", "board_list"), {})

    assert outcome.status == "ok" and "button" in outcome.text
    assert [p["tool"] for p in proposed] == ["board_list"]


async def test_as_lead_tools_a_refusal_is_named_and_gives_nothing() -> None:
    from app.mcp.specs import mcp_name
    from app.tools.pipeline import ToolPipeline

    tools = Tools({"board_list": ToolRefused("tool_refused")})
    used: list[dict[str, Any]] = []
    pipeline = ToolPipeline(room_tools.room_specs(tools, OFFERED, used, []))  # type: ignore[arg-type]

    outcome = await pipeline.call(mcp_name("Boards", "board_list"), {})

    assert outcome.status == "refused"
    assert used == [{"server": "Boards", "tool": "board_list", "outcome": "refused"}]


async def test_as_lead_tools_a_broken_server_is_named_failed() -> None:
    from app.mcp.specs import mcp_name
    from app.tools.pipeline import ToolPipeline

    tools = Tools({"board_list": McpError("unreachable")})
    used: list[dict[str, Any]] = []
    pipeline = ToolPipeline(room_tools.room_specs(tools, OFFERED, used, []))  # type: ignore[arg-type]

    # The lead's loop tells the model it failed; the reply's line names it.
    with pytest.raises(McpError):
        await pipeline.call(mcp_name("Boards", "board_list"), {})

    assert used == [{"server": "Boards", "tool": "board_list", "outcome": "failed"}]
