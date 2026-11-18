"""One round of calls to the tools the owner granted, before an agent answers.

The agents do not use native function calling: each prepares its material and
then answers once. So a tool is offered as a choice made before the answer —
one completion that names the calls, then the calls, then their text — rather
than mid-answer.

A tool is optional material. A malformed choice, a refused call or a server
that is down costs that tool, never the agent's work. Only a question for the
owner stops the work, because that is the owner's to answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from time import monotonic
from typing import TYPE_CHECKING, Any, NamedTuple

from app.context import ProjectContext
from app.gateway.client import GatewayError
from app.mcp.calling import McpTools
from app.mcp.specs import mcp_name, mcp_specs
from app.tools.choice import Call, listing, parse_calls
from app.tools.pipeline import ToolPipeline

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.agents.base import BaseAgent

# How much of each result reaches the prompt.
RESULT_CHARS = 12_000
SLUG = re.compile(r"[^a-z0-9-]+")


class ToolEvidence(NamedTuple):
    """What one call returned, and the address it is cited by."""

    address: str
    label: str
    text: str


def tool_address(server: str, tool: str, arguments: dict[str, Any]) -> str:
    """A citable address for one tool call: the same call, the same address.

    Parallel subtasks merge their sources, so two subtasks making one call must
    cite one source; different arguments are different evidence.
    """
    digest = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    host = SLUG.sub("-", server.lower()).strip("-") or "server"
    return f"mcp://{host}/{SLUG.sub('-', tool.lower()).strip('-') or 'tool'}/{digest}"


async def gather(
    agent: BaseAgent[Any],
    tools: McpTools | None,
    question: str,
    context: ProjectContext,
    *,
    purpose: str,
    limit: int,
    allowed_only: bool = False,
) -> list[ToolEvidence]:
    """Ask the agent's model which calls would help, make them, return their text.

    `purpose` tells the model what a call is for. `allowed_only` offers only
    tools the owner allowed outright, never one that stops for approval.
    """
    if tools is None:
        return []
    offered = [
        tool for tool in tools.offered() if not (allowed_only and tool.get("needs_approval"))
    ]
    if not offered:
        return []
    calls = await _choose(agent, question, offered, context, purpose, limit)
    pipeline = ToolPipeline(mcp_specs(tools, offered))
    evidence: list[ToolEvidence] = []
    seen: set[str] = set()
    for server, name, arguments in calls:
        address = tool_address(server, name, arguments)
        if address in seen:
            continue
        seen.add(address)
        try:
            outcome = await pipeline.call(mcp_name(server, name), arguments)
        except GatewayError:
            raise  # a paid call past the ceiling pauses the task for the owner
        except Exception:  # noqa: BLE001 - a broken server costs its own call only
            logger.warning('{"event":"tool_round_failed","tool":"%s"}', name, exc_info=True)
            continue
        if outcome.status == "ask" and outcome.error is not None:
            # Only a question for the owner stops the work.
            raise outcome.error
        if outcome.status != "ok":
            continue
        evidence.append(ToolEvidence(address, f"{server} · {name}", outcome.text[:RESULT_CHARS]))
    return evidence


async def _choose(
    agent: BaseAgent[Any],
    question: str,
    offered: list[dict[str, Any]],
    context: ProjectContext,
    purpose: str,
    limit: int,
) -> list[Call]:
    """One completion naming the calls to make. Billed like any other."""
    tools = listing(offered, approval_note=" (stops for the owner to approve)")
    messages = [
        {
            "role": "system",
            "content": (
                f"You decide which of the owner's granted tools to call {purpose}. Call a "
                "tool only when its result would be evidence for the question. At most "
                f"{limit} calls. A paid tool spends the owner's money on every call: call "
                "one only when no free tool would give the same evidence. Return JSON only: "
                '{"calls": [{"server": "...", "tool": "...", "arguments": {...}}]}, '
                'or {"calls": []} when no tool would help.'
            ),
        },
        {"role": "user", "content": f"Question:\n{question}\n\nTools:\n{tools}"},
    ]
    started = monotonic()
    try:
        completion = await agent.gateway.complete(agent.model_alias, messages)
    except GatewayError:
        # Choosing a tool is optional; the agent's own work may still succeed.
        return []
    agent._emit(context, "ok", monotonic() - started, completion=completion)
    return parse_calls(completion.content, offered, limit=limit)
