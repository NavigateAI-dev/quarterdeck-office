"""MCP tools as ToolSpecs: each runs through McpTools.call."""

from typing import Any

from app.mcp.specs import mcp_name, mcp_specs
from app.tools.pipeline import ToolPipeline

OFFERED = [
    {
        "server": "crm",
        "name": "search",
        "description": "Find contacts",
        "schema": {"type": "object"},
    },
    {"server": "web", "name": "search", "description": None},
]


class Tools:
    def __init__(self) -> None:
        self.made: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, server: str, name: str, arguments: dict[str, Any]) -> str:
        self.made.append((server, name, arguments))
        return f"{server} found it"


def test_two_servers_may_offer_tools_with_one_name() -> None:
    specs = mcp_specs(Tools(), OFFERED)  # type: ignore[arg-type]

    assert [s.name for s in specs] == ["mcp:crm:search", "mcp:web:search"]
    assert specs[0].input_schema == {"type": "object"}
    assert specs[1].description == ""


async def test_a_call_goes_through_mcp_tools_with_its_arguments_as_given() -> None:
    tools = Tools()
    pipeline = ToolPipeline(mcp_specs(tools, OFFERED))  # type: ignore[arg-type]

    outcome = await pipeline.call(mcp_name("web", "search"), {"q": 1, "extra": True})

    assert tools.made == [("web", "search", {"q": 1, "extra": True})]
    assert outcome.text == "web found it"


def test_a_tool_offered_twice_keeps_its_first_entry() -> None:
    twice = [OFFERED[0], {**OFFERED[0], "description": "later"}]

    [spec] = mcp_specs(Tools(), twice)  # type: ignore[arg-type]

    assert spec.description == "Find contacts"
