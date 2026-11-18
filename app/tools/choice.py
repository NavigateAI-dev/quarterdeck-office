"""The one way a model is offered the owner's granted tools and its choice is read.

Tasks (`agents/tool_round`) and discussions (`room_tools`) both ask a model,
in one completion, which tools to call before it answers. They differ in why
the tools are offered and in what a tool that asks the owner first does, so
those are the callers' words; the listing and the reading are shared.

A choice is held to what was offered, not to what a server would accept: a
refused or unoffered tool the model names anyway is never sent, and never
takes the place of one that was offered.
"""

from __future__ import annotations

import json
from typing import Any

from app.gateway.client import strip_code_fence

Call = tuple[str, str, dict[str, Any]]


def listing(offered: list[dict[str, Any]], *, approval_note: str) -> str:
    """One entry per tool: server, name, description, approval, price and schema."""
    return "\n".join(
        f"- server {tool['server']!r}, tool {tool['name']!r}: "
        f"{tool.get('description') or 'no description'}"
        f"{approval_note if tool.get('needs_approval') else ''}"
        f"{_priced(tool.get('price_usd'))}\n"
        f"  arguments schema: {json.dumps(tool.get('schema') or {}, separators=(',', ':'))}"
        for tool in offered
    )


def _priced(price: Any) -> str:
    """What a call costs, said beside the tool so the model weighs it."""
    return f" (paid: up to ${price:.2f} per call)" if isinstance(price, int | float) else ""


def parse_calls(text: str, offered: list[dict[str, Any]], *, limit: int) -> list[Call]:
    """The calls the model named, held to what was offered, at most `limit`."""
    try:
        payload = json.loads(strip_code_fence(text))
    except json.JSONDecodeError:
        return []
    raw = payload.get("calls") if isinstance(payload, dict) else None
    on_offer = {(str(tool["server"]), str(tool["name"])) for tool in offered}
    calls: list[Call] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        server, name, arguments = item.get("server"), item.get("tool"), item.get("arguments")
        if isinstance(server, str) and isinstance(name, str) and (server, name) in on_offer:
            calls.append((server, name, arguments if isinstance(arguments, dict) else {}))
    return calls[:limit]
