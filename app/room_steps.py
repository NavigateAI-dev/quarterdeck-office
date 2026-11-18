"""What an agent is doing while it replies in a discussion, and what it did.

The steps sit on the room's "replying" entry while it works, for the page to
read, and the finished ones are kept on the reply. Each names a tool and how it
went, never its arguments. A connected tool is named on the reply by
`tools_used`, with what happened at its server, so it is not kept twice.
"""

from __future__ import annotations

from typing import Any

RUNNING = "running"
STOPPED = "stopped"
READ_CONVERSATION = "get_conversation"
MAX_SOURCES = 12
REASON_CHARS = 120


class StepLog:
    def __init__(self, steps: list[dict[str, Any]] | None = None) -> None:
        self.steps = steps if steps is not None else []

    async def started(self, event: dict[str, Any]) -> None:
        self.steps.extend({"tool": tool, "status": RUNNING} for tool in event.get("tools", []))

    async def step(self, event: dict[str, Any]) -> None:
        if event.get("kind") != "tool_call":
            return
        done = {
            "tool": event["tool"],
            "status": event["status"],
            "reason": str(event.get("reason") or "")[:REASON_CHARS],
            "source": str(event.get("source") or ""),
        }
        for step in self.steps:
            if step["tool"] == done["tool"] and step["status"] == RUNNING:
                step.update(done)
                return
        # A delegated brief's tools report only when they finish.
        self.steps.append(done)

    def kept(self) -> list[dict[str, Any]]:
        """The steps for the reply; one still running when the reply came never finished."""
        return [
            {**step, "status": STOPPED} if step["status"] == RUNNING else dict(step)
            for step in self.steps
            if not str(step["tool"]).startswith("mcp:")
        ]

    def conversations(self) -> list[str]:
        """The AI history conversations really read for this reply."""
        return list(
            dict.fromkeys(
                str(step["source"])
                for step in self.steps
                if step["tool"] == READ_CONVERSATION and step.get("source")
            )
        )


def sources_action(cited: list[str], unresolved: list[str], log: StepLog) -> dict[str, Any] | None:
    """What the reply rests on: addresses it cites that a tool returned, and history read."""
    addresses = [address for address in cited if address not in unresolved][:MAX_SOURCES]
    conversations = log.conversations()[:MAX_SOURCES]
    if not addresses and not conversations:
        return None
    return {"kind": "sources", "addresses": addresses, "conversations": conversations}
