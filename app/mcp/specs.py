"""The owner's granted MCP tools as `ToolSpec`s.

Each offered tool runs through `McpTools.call`, which checks the owner's
stance, the office's policies and the price before the transport. A server
checks its own arguments, so a spec carries the server's schema to show the
model and no input model of the office's.
"""

from __future__ import annotations

from typing import Any

from app.mcp.calling import McpTools
from app.tools.pipeline import ToolResult, ToolSpec


def mcp_name(server: str, tool: str) -> str:
    """The pipeline name of one server's tool; servers may share tool names."""
    return f"mcp:{server}:{tool}"


def mcp_specs(tools: McpTools, offered: list[dict[str, Any]]) -> list[ToolSpec]:
    """One spec per offered tool, each calling through `tools`.

    A name offered twice keeps its first entry, the one `McpTools.call` would
    reach.
    """
    specs: dict[str, ToolSpec] = {}
    for row in offered:
        server, tool = str(row["server"]), str(row["name"])
        specs.setdefault(mcp_name(server, tool), _spec(tools, server, tool, row))
    return list(specs.values())


def _spec(tools: McpTools, server: str, tool: str, row: dict[str, Any]) -> ToolSpec:
    async def run(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(text=await tools.call(server, tool, arguments))

    return ToolSpec(
        name=mcp_name(server, tool),
        description=str(row.get("description") or ""),
        run=run,
        schema=dict(row.get("schema") or {}),
    )
