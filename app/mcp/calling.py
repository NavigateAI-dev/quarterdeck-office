"""Calling an MCP tool, through everything that may refuse it.

Four things stand between an agent and a third party's tool, and they are
deliberately separate, because each answers a different question:

1. **The owner's stance** — may this tool be called at all? Unreviewed,
   changed and refused tools never reach the transport. A tool marked as
   needing approval is *proposed*: the run stops and the owner decides.
2. **The office's policies** — `TOOL_CALL` with ALLOW, DENY and ASK, layered
   office then project, exactly as web fetching and search already are. A
   policy can refuse a call the owner allowed in general.
3. **The transport** (`client.py`) — which grants nothing and only sends.

Every call is recorded as a run event, so a third party's tool reaching into a
run is as visible as anything else the office did. It is not counted against the
run's token ceiling, because an MCP call spends none; the minute ceiling bounds
it as it bounds every other step. A tool the owner priced is a fourth check,
`metering.py`: its price counts against the task's cost ceiling and the monthly
limits, and is recorded as spending.

The order matters. The owner's stance is checked first so that a tool they
refused is never mentioned to a policy, never recorded as attempted, and never
sent anywhere. A refusal should be quiet.

What comes back is text and nothing else. A server that fails is a failed call,
not a failed run: an agent is told the tool failed and carries on, because a
third party being down is not a reason to throw away the work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

from app.mcp import review as mcp_review
from app.mcp.client import McpClient, McpError, ToolResult
from app.mcp.metering import NOT_SENT, ToolMeter
from app.policies.engine import Hook
from app.side_effects import DONE, FAILED, UNKNOWN, RunWrites

# How much of a call's arguments an approval question shows, and the length the
# policy record allows for what was asked about.
ASKED_ARGUMENTS = 600
SUBJECT_LENGTH = 64
# Hex digits of the digest that makes an approval subject unique (80 bits).
SUBJECT_DIGEST = 20
# Sent, with no answer: whether the tool ran is not known.
NO_ANSWER = frozenset({"no_answer"})
# What an agent is told instead of a result, when a call is not sent again.
ALREADY_MADE = (
    "This call to {tool} on {server} was already made earlier in this task, before the office "
    "restarted. It is not made again; its result was not kept."
)
MAYBE_MADE = (
    "The call to {tool} on {server} may or may not have gone through: the office stopped while "
    "making it. It is not made again; the owner has been asked to check."
)


class ToolRefused(RuntimeError):
    """The office will not make this call. Carries a short reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ApprovalRequired(RuntimeError):
    """The owner marked this tool as one they decide, call by call."""

    def __init__(self, server: str, tool: str) -> None:
        super().__init__(f"{server}:{tool}")
        self.server = server
        self.tool = tool


@dataclass(frozen=True)
class Reachable:
    """One server an agent may use in this run, as the office resolved it."""

    server_id: UUID
    name: str
    url: str
    token: str | None
    tools: list[dict[str, Any]]
    review: dict[str, Any]

    def tool(self, name: str) -> dict[str, Any] | None:
        return next((row for row in self.tools if row.get("name") == name), None)


class McpTools:
    """The MCP tools one run may use, and the checks in front of them."""

    def __init__(
        self,
        servers: list[Reachable],
        gate: Any,
        run_id: UUID | None,
        project_id: UUID | None,
        allow_list: list[str],
        client: McpClient | None = None,
        record: Callable[[str, dict[str, Any]], Any] | None = None,
        meter: ToolMeter | None = None,
        writes: RunWrites | None = None,
    ) -> None:
        self.servers = servers
        self.gate = gate
        self.run_id = run_id
        self.project_id = project_id
        self.allow_list = allow_list
        self.client = client or McpClient()
        # What was called, and how much came back. A third party's tool reaching
        # into a run should be as visible as anything else the office did.
        self.record = record
        # Counts priced calls against the limits; None where nothing is priced.
        self.meter = meter
        # Each call written down before it is sent: a run picked up
        # again after a restart does not send it twice.
        self.writes = writes

    def offered(self) -> list[dict[str, Any]]:
        """What an agent may be told it can use: name, server and schema."""
        described = []
        for server in self.servers:
            for tool in mcp_review.callable_tools(server.tools, server.review):
                described.append(
                    {
                        "server": server.name,
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "schema": tool.get("schema") or {},
                        # So an agent knows which calls will stop for the owner.
                        "needs_approval": mcp_review.needs_approval(tool, server.review),
                        # So an agent spends only where the evidence is worth it.
                        "price_usd": mcp_review.price_of(server.review, tool),
                    }
                )
        return described

    async def call(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call one tool, or refuse. Returns the text the tool returned."""
        server = next((row for row in self.servers if row.name == server_name), None)
        if server is None:
            raise ToolRefused("server_not_granted")
        tool = server.tool(tool_name)
        if tool is None:
            raise ToolRefused("tool_not_offered")

        # 1. The owner's stance, before anything else knows the call exists.
        stance = mcp_review.stance_of(server.review, tool)
        if stance not in (mcp_review.ALLOWED, mcp_review.NEEDS_APPROVAL):
            raise ToolRefused(f"tool_{stance}")
        if stance == mcp_review.NEEDS_APPROVAL:
            # Not a refusal: the owner decides this one. The server's own claim
            # that the tool is read-only does not change it.
            self._ask_owner(server, tool_name, arguments)

        # 2. The office's policies, exactly as for fetching and search.
        self.gate.enforce(
            Hook.TOOL_CALL,
            "mcp_call",
            run_id=self.run_id,
            project_id=self.project_id,
            server=server.name,
            tool=tool_name,
        )

        # 3. The price, when the owner gave one: within the limits, or not sent.
        price = mcp_review.price_of(server.review, tool)
        metered = self.meter if price is not None else None
        if metered is not None and price is not None:
            refused = metered.admit(price)
            if refused:
                self._note(server.name, tool_name, outcome=refused, characters=0)
                raise ToolRefused(refused)
        started = monotonic()

        # 4. Written down before it is sent, unless it already was.
        entry: int | None = None
        if self.writes is not None:
            entry, status = self.writes.intend("mcp_call", server.name, tool_name, arguments)
            if status == DONE:
                self._note(server.name, tool_name, outcome="already_made", characters=0)
                return ALREADY_MADE.format(tool=tool_name, server=server.name)
            if status == UNKNOWN:
                self._note(server.name, tool_name, outcome="outcome_unknown", characters=0)
                return MAYBE_MADE.format(tool=tool_name, server=server.name)

        # 5. The transport, which grants nothing.
        try:
            result: ToolResult = await self.client.call(
                server.url, server.token, self.allow_list, tool_name, arguments
            )
        except McpError as failed:
            # A third party being down is not a reason to lose the run.
            if metered is not None and price is not None and failed.code not in NOT_SENT:
                metered.charge(server.name, tool_name, price, failed.code, started)
            if entry is not None and self.writes is not None:
                settled = UNKNOWN if failed.code in NO_ANSWER else FAILED
                self.writes.settle(entry, settled, failed.code)
            self._note(server.name, tool_name, outcome=failed.code, characters=0)
            return f"The tool {tool_name} on {server.name} could not be called: {failed.code}."
        outcome = "tool_failed" if result.failed else "ok"
        if entry is not None and self.writes is not None:
            self.writes.settle(entry, FAILED if result.failed else DONE, outcome)
        if metered is not None and price is not None:
            metered.charge(server.name, tool_name, price, outcome, started)
        if result.failed:
            self._note(server.name, tool_name, outcome=outcome, characters=0)
            return f"The tool {tool_name} on {server.name} reported a failure."
        self._note(server.name, tool_name, outcome=outcome, characters=len(result.content))
        return result.content

    def _ask_owner(self, reachable: Reachable, tool: str, arguments: dict[str, Any]) -> None:
        """Pause the task with a question, unless the owner already allowed this
        tool for this task. A gate that cannot ask refuses instead."""
        server = reachable.name
        ask = getattr(self.gate, "ask_owner", None)
        if ask is None:
            raise ApprovalRequired(server, tool)
        # The owner decides on what would be sent, so the question shows it.
        # It is kept only because it was asked: an allowed call's arguments are not.
        sent = json.dumps(arguments, sort_keys=True, default=str)
        if len(sent) > ASKED_ARGUMENTS:
            sent = sent[:ASKED_ARGUMENTS] + "…"
        ask(
            approval_subject(reachable.server_id, server, tool),
            f"This task wants to call {tool} on {server}, which you decide. It would send {sent}.",
            "mcp_call",
            run_id=self.run_id,
            project_id=self.project_id,
            server=server,
            tool=tool,
        )

    def _note(self, server: str, tool: str, outcome: str, characters: int) -> None:
        """Record the call. The arguments are not kept: they are model output
        bound for a third party, and the office has no business storing them."""
        if self.record is None:
            return
        self.record(
            "mcp_call",
            {"server": server, "tool": tool, "outcome": outcome, "characters": characters},
        )


def approval_subject(server_id: UUID, server: str, tool: str) -> str:
    """What an owner's allow for one tool is recorded under.

    The readable part is for the owner's list of rules and may be cut short; the
    digest of the server's id and the whole tool name is what keeps one tool's
    allow from covering another, however long or alike their names are.
    """
    identity = json.dumps([str(server_id), tool])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:SUBJECT_DIGEST]
    readable = f"mcp:{server}:{tool}"[: SUBJECT_LENGTH - SUBJECT_DIGEST - 1]
    return f"{readable}#{digest}"
