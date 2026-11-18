"""The policies the office ships with.

Each one is small, says what it refuses and why, and reads only what the event
carries. A policy may keep counters for the run it is evaluating, so a rule can
depend on what has already happened.
"""

from typing import Any
from urllib.parse import urlparse

from app.policies.engine import (
    Decision,
    Hook,
    PolicyEvent,
    PolicyKind,
    Verdict,
    has_allowance,
    register,
)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _matches(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower().lstrip("*.")
    return bool(pattern) and (host == pattern or host.endswith(f".{pattern}"))


def no_web(event: PolicyEvent, params: dict[str, Any]) -> Decision | None:
    if event.target != "web_fetch" and event.target != "web_search":
        return None
    return Decision(
        Verdict.DENY,
        "This project works from its own documents; the web is not available to it.",
        "no_web",
    )


def web_allow_list(event: PolicyEvent, params: dict[str, Any]) -> Decision | None:
    if event.target != "web_fetch":
        return None
    hosts = [str(item) for item in params.get("hosts", []) if str(item).strip()]
    if not hosts:
        return None
    host = _host(str(event.data.get("url", "")))
    if any(_matches(host, pattern) for pattern in hosts):
        return None
    return Decision(
        Verdict.DENY,
        f"{host or 'that address'} is not on this office's list of allowed sites.",
        "web_allow_list",
    )


def ask_before_web(event: PolicyEvent, params: dict[str, Any]) -> Decision | None:
    """The first page a task fetches waits for the owner; the rest follow."""
    if event.target != "web_fetch":
        return None
    if has_allowance(event, "ask_before_web") or int(event.state.get("web_allowed", 0)) > 0:
        return None
    host = _host(str(event.data.get("url", "")))
    return Decision(
        Verdict.ASK,
        f"This task wants to read {host or 'a web page'}.",
        "ask_before_web",
    )


def max_tool_calls(event: PolicyEvent, params: dict[str, Any]) -> Decision | None:
    if event.hook is not Hook.TOOL_CALL:
        return None
    limit = int(params.get("limit", 200))
    used = int(event.state.get("tool_calls", 0))
    if used >= limit:
        return Decision(
            Verdict.DENY,
            f"This task has already made {used} tool calls, its limit.",
            "max_tool_calls",
        )
    return Decision(Verdict.ALLOW, "", "max_tool_calls", add={"tool_calls": 1})


def expensive_models(event: PolicyEvent, params: dict[str, Any]) -> Decision | None:
    """Keep named models for work the owner has agreed to pay for."""
    if event.hook is not Hook.MODEL_CALL:
        return None
    named = [str(item).lower() for item in params.get("models", [])]
    model = str(event.data.get("model", "")).lower()
    if not named or not any(item in model for item in named):
        return None
    if has_allowance(event, "expensive_models"):
        return None
    return Decision(
        Verdict.ASK,
        f"{event.data.get('model')} is on the office's list of models to confirm first.",
        "expensive_models",
    )


def risk_score(event: PolicyEvent, params: dict[str, Any]) -> Decision | None:
    """Judge a task by what it has done so far, not only by this one step."""
    if event.hook is not Hook.TOOL_CALL:
        return None
    weights: dict[str, int] = {
        "web_fetch": 1,
        "web_search": 1,
        "local_documents": 0,
        "engineering": 5,
        **{str(k): int(v) for k, v in (params.get("weights") or {}).items()},
    }
    ask_at = int(params.get("ask_at", 25))
    score = int(event.state.get("risk", 0)) + weights.get(event.target, 1)
    if score >= ask_at and not has_allowance(event, "risk_score"):
        return Decision(
            Verdict.ASK,
            f"This task has done a lot ({score} points of risk); it is pausing for you.",
            "risk_score",
            add={"risk": weights.get(event.target, 1)},
        )
    return Decision(Verdict.ALLOW, "", "risk_score", add={"risk": weights.get(event.target, 1)})


BUILT_IN = [
    PolicyKind(
        id="no_web",
        title="No web access",
        about="Tasks work from their own documents. Web search and page reading are refused.",
        hooks=(Hook.TOOL_CALL,),
        parameters={},
        evaluate=no_web,
    ),
    PolicyKind(
        id="web_allow_list",
        title="Only these websites",
        about="Page reading is refused unless the address is on the list.",
        hooks=(Hook.TOOL_CALL,),
        parameters={"hosts": {"type": "list", "of": "host", "example": "docs.example.com"}},
        evaluate=web_allow_list,
    ),
    PolicyKind(
        id="ask_before_web",
        title="Ask before reading the web",
        about="The first page a task wants to read waits for you; after that it continues.",
        hooks=(Hook.TOOL_CALL,),
        parameters={},
        evaluate=ask_before_web,
    ),
    PolicyKind(
        id="max_tool_calls",
        title="Limit tool use in one task",
        about="Refuses further tool calls once a task has made this many.",
        hooks=(Hook.TOOL_CALL,),
        parameters={"limit": {"type": "number", "default": 200}},
        evaluate=max_tool_calls,
    ),
    PolicyKind(
        id="expensive_models",
        title="Confirm costly models",
        about="Pauses for you before a named model is used.",
        hooks=(Hook.MODEL_CALL,),
        parameters={"models": {"type": "list", "of": "text", "example": "opus"}},
        evaluate=expensive_models,
    ),
    PolicyKind(
        id="risk_score",
        title="Pause a task that does a lot",
        about="Counts what a task has done and pauses for you once it passes a threshold.",
        hooks=(Hook.TOOL_CALL,),
        parameters={"ask_at": {"type": "number", "default": 25}},
        evaluate=risk_score,
    ),
]

for kind in BUILT_IN:
    register(kind)
