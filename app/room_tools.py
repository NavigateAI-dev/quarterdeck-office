"""Tools in discussions.

An agent replying in a discussion (the lead, on the agent's own model) may
call the tools the owner granted the discussion's project, or ticked for
discussions with no project, through every check (the owner's review, the
office's policies), as its own tools.

Only tools the owner allowed outright run on their own. A tool that asks the
owner first becomes a button under the reply: the owner's press is the
approval, and the result comes back into the discussion. Paid tools are left
to tasks, where a cost ceiling counts them. Every call is named on the reply.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.mcp import review as mcp_review
from app.mcp import store as mcp_store
from app.mcp.calling import ApprovalRequired, McpTools, ToolRefused
from app.mcp.client import McpClient, McpError
from app.mcp.specs import mcp_specs
from app.policies.engine import Hook, Verdict
from app.policies.store import PolicyAsk, PolicyGate, PolicyRefused
from app.secret_store import SecretStoreError
from app.tools.pipeline import ToolResult, ToolSpec

logger = logging.getLogger(__name__)

RESULT_CHARS = 8_000
SHOWN_CHARS = 4_000


async def tools_for(
    session_factory: sessionmaker[Session], settings: Any, project_id: UUID | None
) -> McpTools | None:
    """The tools granted to the project, or with no project those the owner
    ticked for such discussions, with credentials resolved; None if none."""
    from app.mcp.oauth import OAuthError

    allow_list = mcp_store.allow_hosts(settings)
    with session_factory() as session:
        granted = mcp_store.reachable_for(session, project_id)
    servers = []
    for server, reference in granted:
        try:
            token = await mcp_store.bearer_for(reference, settings, allow_list)
        except (SecretStoreError, OAuthError):
            continue
        servers.append(replace(server, token=token))
    if not servers:
        return None
    return McpTools(servers, PolicyGate(session_factory), None, project_id, allow_list)


def offered(tools: McpTools) -> list[dict[str, Any]]:
    """What a discussion may use: free tools the owner allowed or decides."""
    return [tool for tool in tools.offered() if tool.get("price_usd") is None]


def room_specs(
    tools: McpTools,
    offered_tools: list[dict[str, Any]],
    used: list[dict[str, str]],
    proposed: list[dict[str, Any]],
) -> list[ToolSpec]:
    """The granted tools as the lead's tools in a discussion.

    A tool that asks the owner first, or that an office rule says to ask about,
    becomes a button under the reply and is never sent; every call is named on
    the reply in `used`, a broken server's as failed.
    """
    asks_first = {
        (str(tool["server"]), str(tool["name"]))
        for tool in offered_tools
        if tool.get("needs_approval")
    }
    specs = []
    for spec in mcp_specs(tools, offered_tools):
        server, name = spec.name.removeprefix("mcp:").rsplit(":", 1)
        specs.append(replace(spec, run=_room_run(tools, server, name, asks_first, used, proposed)))
    return specs


def _room_run(
    tools: McpTools,
    server: str,
    name: str,
    asks_first: set[tuple[str, str]],
    used: list[dict[str, str]],
    proposed: list[dict[str, Any]],
) -> Any:
    async def run(arguments: dict[str, Any]) -> ToolResult:
        if (server, name) not in asks_first:
            try:
                text = await tools.call(server, name, arguments)
            except PolicyAsk:
                pass  # the office's rules want the owner asked: the owner's press asks
            except (ToolRefused, ApprovalRequired, PolicyRefused):
                used.append({"server": server, "tool": name, "outcome": "refused"})
                raise
            except McpError:
                used.append({"server": server, "tool": name, "outcome": "failed"})
                raise
            else:
                used.append({"server": server, "tool": name, "outcome": "ok"})
                return ToolResult(text=text[:RESULT_CHARS])
        proposed.append(_proposal(server, name, arguments))
        return ToolResult(
            text="Proposed to the owner as a button under your reply; it has not run. "
            "Say what it would do; never claim it is done."
        )

    return run


def _proposal(server: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "tool_call", "server": server, "tool": name, "arguments": arguments}


async def pressed(
    session_factory: sessionmaker[Session],
    settings: Any,
    project_id: UUID | None,
    action: dict[str, Any],
    owner: str,
    client: McpClient | None = None,
) -> str:
    """Make one proposed call because the owner pressed for it.

    The press is the approval the tool asks for. The owner's review still
    decides (a tool refused since is not sent) and so do the office's rules,
    except that their "ask the owner" is answered by this press.
    """
    tools = await tools_for(session_factory, settings, project_id)
    if tools is None:
        raise ToolRefused("server_not_granted")
    server = next((row for row in tools.servers if row.name == action.get("server")), None)
    tool = server.tool(str(action.get("tool"))) if server is not None else None
    if server is None or tool is None:
        raise ToolRefused("tool_not_offered")
    stance = mcp_review.stance_of(server.review, tool)
    if stance not in (mcp_review.ALLOWED, mcp_review.NEEDS_APPROVAL):
        raise ToolRefused(f"tool_{stance}")
    if mcp_review.price_of(server.review, tool) is not None:
        raise ToolRefused("tool_paid")
    decision = tools.gate.check(
        Hook.TOOL_CALL,
        "mcp_call",
        project_id=project_id,
        server=server.name,
        tool=str(action.get("tool")),
        pressed_by=owner,
    )
    if decision.verdict is Verdict.DENY:
        raise ToolRefused("policy_refused")
    given = action.get("arguments")
    arguments: dict[str, Any] = given if isinstance(given, dict) else {}
    try:
        result = await (client or tools.client).call(
            server.url, server.token, tools.allow_list, str(action.get("tool")), arguments
        )
    except McpError as failed:
        return f"{action.get('tool')} on {server.name} could not be called: {failed.code}."
    if result.failed:
        return f"{action.get('tool')} on {server.name} reported a failure."
    return result.content


def shown(server: str, tool: str, result: str) -> str:
    """The result as the discussion shows it, cut to a readable length."""
    text = result.strip()
    if len(text) > SHOWN_CHARS:
        text = text[:SHOWN_CHARS] + "\n[cut short]"
    return f"Ran {tool} on {server}, as you allowed:\n{text}"
