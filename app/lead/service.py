"""The lead's front door: one request in, one checked answer out.

The task box, discussions and Slack all come here. The caller
gives the request, the context, the work tools the owner granted, and each
role's model already routed, budgeted and guarded; the lead runs on the Chief
role's model, as the office's final answers do today.

The answer's citations are checked in code. An address no tool returned
is sent back to the lead once to fix; one still unresolved after that is
flagged on the reply, never hidden.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.lead.checks import CitationCheck, check_citations
from app.lead.loop import Step, Stopped, run_lead
from app.lead.prompt import Section, lead_messages
from app.lead.spill import Spill
from app.lead.tools import (
    MAX_DELEGATES,
    Brief,
    LeadDesk,
    SharedNotes,
    StepResult,
    TodoItem,
    lead_tools,
)
from app.policies.store import PolicyAsk
from app.tools.pipeline import ToolPipeline, ToolSpec

LEAD_ROLE = "chief"
# Model calls the lead may make on one request, before its best answer so far is given.
LEAD_STEPS = 40


@dataclass
class LeadRequest:
    request: str
    completer_for: Callable[[str], tuple[Any, str]]
    work: list[ToolSpec]
    now: datetime
    doctrine: str = ""
    sections: list[Section] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    max_delegates: int = MAX_DELEGATES
    max_steps: int | None = LEAD_STEPS
    on_step: Step | None = None
    # Told which tools are about to run, for a live view of the work.
    on_start: Step | None = None
    # The lead's model reads pictures (the office's gateway does).
    sees: bool = False
    # Notes the owner sends while it works, and a pause or cancel.
    inbox: Callable[[], list[str]] | None = None
    halt: Callable[[], str | None] | None = None
    on_todo: Callable[[list[TodoItem]], None] | None = None
    on_result: Callable[[Brief, StepResult], None] | None = None
    # Addresses tools returned earlier in this task, before it was paused.
    returned_before: list[str] = field(default_factory=list)


@dataclass
class LeadReply:
    text: str
    stopped: Stopped
    reason: str
    calls: int
    citations: CitationCheck
    todo: list[TodoItem]
    results: list[StepResult]
    proposals: list[dict[str, str]]
    asked: PolicyAsk | None = None


async def answer(asked: LeadRequest) -> LeadReply:
    """Run the lead on one request and check what it says before it is shown."""
    completer, alias = asked.completer_for(LEAD_ROLE)
    notes = SharedNotes(asked.inbox) if asked.inbox is not None else None
    desk = LeadDesk(
        completer_for=asked.completer_for,
        max_delegates=asked.max_delegates,
        on_todo=asked.on_todo,
        on_result=asked.on_result,
        on_step=asked.on_step,
        halt=asked.halt,
        notes=notes,
    )
    tools = ToolPipeline(lead_tools(asked.work, desk))
    spill = Spill()
    messages = lead_messages(
        doctrine=asked.doctrine,
        sections=asked.sections,
        history=asked.history,
        request=asked.request,
        now=asked.now,
    )
    options: dict[str, Any] = {
        "spill": spill,
        "max_steps": asked.max_steps,
        "on_step": asked.on_step,
        "on_start": asked.on_start,
        "sees": asked.sees,
        "inbox": notes.reader() if notes is not None else None,
        "halt": asked.halt,
    }
    result = await run_lead(completer, alias, messages, tools, **options)
    calls = result.steps
    citations = check_citations(result.answer, result.messages, asked.returned_before)
    if result.stopped == "answered" and not citations.ok:
        fix = (
            "These addresses in your answer were not returned by any tool in this "
            f"conversation: {', '.join(citations.unresolved)}. Remove them, or look them up, "
            "then give your whole answer again."
        )
        corrected = await run_lead(
            completer, alias, [*result.messages, {"role": "user", "content": fix}], tools, **options
        )
        calls += corrected.steps
        if corrected.stopped == "answered":
            result = corrected
        citations = check_citations(result.answer, result.messages, asked.returned_before)
    return LeadReply(
        text=result.answer,
        stopped=result.stopped,
        reason=result.reason,
        calls=calls,
        citations=citations,
        todo=desk.todo,
        results=desk.results,
        proposals=desk.proposals,
        asked=result.asked,
    )
