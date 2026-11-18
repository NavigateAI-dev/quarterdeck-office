"""The lead agent's loop.

Learned from DSH's agent loop (`core/agent-loop/src/agent.ts`): one model call
either answers or asks for tools; the tools run, their results go back as tool
messages, and the loop goes on until a reply asks for no tool. There is no
planner in front of it: the model sizes the work itself.

What stops it:
- **an answer** — a reply with no tool calls;
- **the limits** — the completer's budget (`budget_exhausted:*`), or the step
  limit its caller sets; the loop ends with the best answer so far
  and says which limit stopped it;
- **an owner question** — a policy that asks the owner; the caller stops the
  task or proposes the call, as it does today;
- **a failed provider** — its code, as every other call reports it.

What it tells the model: arguments that do not fit, named field by field;
a refusal and its reason; a reminder when the same call is made 3, 5 and 8
times. Long results are spilled, not cut. Calls that are safe to run
together run at once; their results keep the model's order.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.gateway.client import Completion, GatewayError, ToolCall
from app.lead.spill import Spill
from app.policies.store import PolicyAsk
from app.tools.pipeline import ToolOutcome, ToolPipeline

logger = logging.getLogger(__name__)

Stopped = Literal["answered", "limit", "asked", "error", "halted"]
Step = Callable[[dict[str, Any]], Awaitable[None]]

REPEAT_REMINDERS = (3, 5, 8)
# Pictures shown to the model after one step's tool results.
PICTURES = 2
# What a limit's answer quotes of the results gathered so far.
GATHERED_CHARS = 4_000
# How a limit's answer opens when the model had said nothing yet.
CUT_SHORT = "I was stopped"
SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
NAME_CHARS = 64
NO_ARGUMENTS = {"type": "object", "properties": {}}


@dataclass
class LeadResult:
    answer: str
    stopped: Stopped
    reason: str = ""
    asked: PolicyAsk | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0


def tool_names(names: Iterable[str]) -> dict[str, str]:
    """Pipeline name -> a name every provider accepts (`^[a-zA-Z0-9_-]{1,64}$`), unique."""
    chosen: dict[str, str] = {}
    taken: set[str] = set()
    for name in names:
        base = (SAFE_NAME.sub("_", name).strip("_") or "tool")[:NAME_CHARS]
        safe, n = base, 1
        while safe in taken:
            n += 1
            suffix = f"_{n}"
            safe = base[: NAME_CHARS - len(suffix)] + suffix
        taken.add(safe)
        chosen[name] = safe
    return chosen


async def run_lead(
    completer: Any,
    model_alias: str,
    messages: list[dict[str, Any]],
    pipeline: ToolPipeline,
    *,
    spill: Spill | None = None,
    max_steps: int | None = None,
    on_step: Step | None = None,
    on_start: Step | None = None,
    sees: bool = False,
    inbox: Callable[[], list[str]] | None = None,
    halt: Callable[[], str | None] | None = None,
) -> LeadResult:
    """Run the loop until the model answers or something stops it.

    `sees`: the model reads pictures, so one a tool returned (the office's
    browser looking at a radar) is shown to it, after the tool results.
    `inbox`: notes the owner sent while it works, taken before each call, as
    DSH's steer queue; `halt`: a pause or cancel the owner asked for, which
    stops the loop before the next call.
    """
    spill = spill or Spill()
    tools = ToolPipeline([*pipeline.specs(), spill.read_more_spec()])
    names = tool_names(spec.name for spec in tools.specs())
    by_safe = {safe: name for name, safe in names.items()}
    offered = [
        {
            "name": names[spec.name],
            "description": spec.description,
            "parameters": spec.input_schema or NO_ARGUMENTS,
        }
        for spec in tools.specs()
    ]
    safe_together = {spec.name for spec in tools.specs() if spec.concurrency_safe}
    repeats: dict[str, int] = {}
    conversation = list(messages)
    steps = 0
    while True:
        if max_steps is not None and steps >= max_steps:
            return _stopped(conversation, "limit", "step_limit", steps)
        stop = halt() if halt is not None else None
        if stop:
            return _stopped(conversation, "halted", stop, steps)
        notes = inbox() if inbox is not None else []
        for note in notes:
            conversation.append({"role": "user", "content": f"The owner, while you work: {note}"})
        if notes:
            await _report(on_step, {"kind": "steered", "count": len(notes)})
        try:
            completion: Completion = await completer.complete(
                model_alias, conversation, tools=offered
            )
        except GatewayError as error:
            stopped: Stopped = "limit" if error.code.startswith("budget_exhausted") else "error"
            return _stopped(conversation, stopped, error.code, steps)
        steps += 1
        if not completion.tool_calls:
            conversation.append({"role": "assistant", "content": completion.content})
            await _report(on_step, {"kind": "answer"})
            return LeadResult(completion.content, "answered", messages=conversation, steps=steps)
        conversation.append(_assistant_turn(completion))
        await _report(
            on_start,
            {
                "kind": "tools_started",
                "tools": [by_safe.get(call.name, call.name) for call in completion.tool_calls],
            },
        )
        outcomes = await _run_calls(completion.tool_calls, tools, by_safe, safe_together)
        seen: list[bytes] = []
        for call, (name, outcome) in zip(completion.tool_calls, outcomes, strict=True):
            if outcome.status == "ask" and isinstance(outcome.error, PolicyAsk):
                await _report(on_step, _call_step(name, outcome))
                return LeadResult(
                    "",
                    "asked",
                    "owner_decides",
                    asked=outcome.error,
                    messages=conversation,
                    steps=steps,
                )
            key = f"{name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
            repeats[key] = repeats.get(key, 0) + 1
            content = _told(call, outcome, spill)
            if repeats[key] in REPEAT_REMINDERS:
                content += (
                    f"\n\n[You have made this same call {repeats[key]} times. Use what you "
                    "already have, or try something different.]"
                )
            conversation.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if outcome.status == "ok" and outcome.picture:
                seen.append(outcome.picture)
            await _report(on_step, _call_step(name, outcome))
        if sees and seen:
            # A tool result carries text only; pictures come after, as the user's.
            conversation.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What the tools showed, as pictures:"},
                        *(_picture(data) for data in seen[:PICTURES]),
                    ],
                }
            )


async def _run_calls(
    calls: list[ToolCall],
    tools: ToolPipeline,
    by_safe: dict[str, str],
    safe_together: set[str],
) -> list[tuple[str, ToolOutcome]]:
    """Each call's outcome, in the model's order; consecutive safe calls run at once."""
    results: list[tuple[str, ToolOutcome]] = []
    batch: list[ToolCall] = []

    async def one(call: ToolCall) -> tuple[str, ToolOutcome]:
        name = by_safe.get(call.name, call.name)
        if call.arguments is None:
            return name, ToolOutcome("invalid", reason="not_json")
        try:
            return name, await tools.call(name, call.arguments)
        except GatewayError:
            raise  # a spent budget stops the work, as it would on a model call
        except Exception as error:  # noqa: BLE001 - a failed tool is told, not fatal
            # A page that will not load or a server that is down is one failed call:
            # the model hears why and answers without it.
            logger.warning('{"event":"lead_tool_failed","tool":"%s"}', name, exc_info=True)
            reason = getattr(error, "code", None) or str(error)[:80] or type(error).__name__
            return name, ToolOutcome("failed", reason=str(reason), error=error)

    async def flush() -> None:
        results.extend(await asyncio.gather(*(one(call) for call in batch)))
        batch.clear()

    for call in calls:
        if by_safe.get(call.name, call.name) in safe_together:
            batch.append(call)
            continue
        await flush()
        results.append(await one(call))
    await flush()
    return results


def _told(call: ToolCall, outcome: ToolOutcome, spill: Spill) -> str:
    """What the model reads back for one call."""
    if outcome.status == "ok":
        return spill.shown(outcome.text)
    if outcome.reason == "not_json":
        return (
            f"Invalid arguments: they were not a JSON object ({call.raw_arguments[:200]!r}). "
            "Call again with a JSON object."
        )
    if outcome.status == "invalid":
        return f"Invalid arguments: {outcome.reason}. Fix them and call again."
    if outcome.status == "refused":
        return f"Refused: {outcome.reason}. Do not make this call again; go on without it."
    return f"Failed: {outcome.reason}."


def _picture(data: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def _assistant_turn(completion: Completion) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": completion.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in completion.tool_calls
        ],
    }


def _call_step(name: str, outcome: ToolOutcome) -> dict[str, Any]:
    step = {"kind": "tool_call", "tool": name, "status": outcome.status, "reason": outcome.reason}
    return {**step, "source": outcome.source} if outcome.source else step


async def _report(on_step: Step | None, event: dict[str, Any]) -> None:
    if on_step is not None:
        await on_step(event)


def _stopped(
    conversation: list[dict[str, Any]], stopped: Stopped, reason: str, steps: int
) -> LeadResult:
    """The best answer so far: the last thing the model said, else what it gathered."""
    said = next(
        (
            str(m.get("content") or "")
            for m in reversed(conversation)
            if m["role"] == "assistant" and str(m.get("content") or "").strip()
        ),
        "",
    )
    if not said:
        gathered = "\n\n".join(
            str(m.get("content") or "") for m in conversation if m["role"] == "tool"
        )
        said = (
            f"{CUT_SHORT} ({reason}) before I could answer. What I found so far:\n\n"
            f"{gathered[:GATHERED_CHARS]}"
            if gathered
            else ""
        )
    return LeadResult(said, stopped, reason, messages=conversation, steps=steps)
