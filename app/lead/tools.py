"""The lead's own tools.

Planning, delegation, review and the full team are tools the lead chooses, as
in DSH (todo, subagent, workflow), not phases every request goes through. Each
says when not to use it: the description is what sizes the work.

- `todo`: a short visible step list for multi-step work.
- `delegate`: focused briefs to researchers or strategists, run at once. Each
  is its own loop with the work tools and a fresh context, one level deep, and
  returns a result — summary, findings, sources, and how it ended — never its
  transcript. A stop or a spent budget reaches every brief, and so does a note
  from the owner; each brief has a step limit of its own.
- `review`: the critic reads a draft before the lead answers.
- `propose_team_run`: offers the full team pipeline; the owner presses Start.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, create_model

from app.gateway.client import strip_code_fence
from app.lead.checks import keep_returned
from app.lead.loop import LeadResult, Step, Stopped, run_lead
from app.tools.pipeline import ToolPipeline, ToolResult, ToolSpec

# DSH's default for sub-agents at once; an owner setting overrides it (§15.3).
MAX_DELEGATES = 8
# Model calls one brief may make: the work is focused, and several run at once.
BRIEF_STEPS = 20
BRIEF_CHARS = 4_000
# A brief's result, by how its loop stopped. An owner question is raised, not reported.
ENDED: dict[Stopped, Literal["done", "limit", "failed", "stopped"]] = {
    "answered": "done",
    "limit": "limit",
    "error": "failed",
    "halted": "stopped",
    "asked": "stopped",
}

ROLE_PROMPTS = {
    "researcher": (
        "You are a researcher working for the office's lead on one focused brief. Use the "
        "tools to find evidence. Start with short, broad searches, see what exists, then "
        "narrow. Prefer primary and official sources to summaries of them, and read a page "
        "before relying on it. Stop as soon as the brief is answered; if it cannot be, say "
        "what you could not find. Report only what the tools returned. Finish with JSON only: "
        '{"summary": "two or three sentences that answer the brief", "findings": ["one '
        'fact per item, with its source"], "sources": ["each web address or tool address '
        'you used"]}.'
    ),
    "strategist": (
        "You are a strategist working for the office's lead on one focused brief. Reason "
        "from the material given and what you can check with the tools. Finish with JSON "
        'only: {"summary": "your recommendation in two or three sentences", "findings": '
        '["each point that supports it"], "sources": ["each source you relied on"]}.'
    ),
}

REVIEW_PROMPT = (
    "You review a draft answer before the office's owner reads it. List each real problem: "
    "a claim without a source, a number that does not follow, a part of the request left "
    "unanswered, a risk not named. Be specific and short. If there is none, say so."
)


class StepResult(BaseModel):
    """What one delegated brief gives back: never its transcript."""

    summary: str
    findings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    # How the brief ended, set by the office and never taken from the model's words.
    status: Literal["done", "limit", "failed", "stopped"] = "done"
    reason: str = ""
    # Addresses it reported that its own tools did not return; never shown to the lead.
    unreturned: list[str] = Field(default_factory=list)


class TodoItem(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    status: Literal["pending", "in_progress", "done"] = "pending"


class Todo(BaseModel):
    items: list[TodoItem] = Field(min_length=1, max_length=20)


class Brief(BaseModel):
    brief: str = Field(
        min_length=1, max_length=BRIEF_CHARS, description="The objective: what to find out."
    )
    role: Literal["researcher", "strategist"] = "researcher"
    output: str = Field(default="", max_length=BRIEF_CHARS, description="What to give back.")
    sources: str = Field(default="", max_length=BRIEF_CHARS, description="Where to look first.")
    boundaries: str = Field(
        default="",
        max_length=BRIEF_CHARS,
        description="What is out of scope, so briefs do not overlap.",
    )


class Review(BaseModel):
    draft: str = Field(min_length=1)
    request: str = ""


class TeamRun(BaseModel):
    objective: str = Field(min_length=1, max_length=600)
    why: str = Field(min_length=1, max_length=600)


class SharedNotes:
    """The owner's notes, for the lead and each brief: the inbox hands a note out once,
    so it is taken here and every reader gets it once."""

    def __init__(self, inbox: Callable[[], list[str]]) -> None:
        self.inbox = inbox
        self.notes: list[str] = []

    def reader(self) -> Callable[[], list[str]]:
        """Reads the notes taken from now on, and any still waiting in the inbox."""
        seen = len(self.notes)

        def read() -> list[str]:
            nonlocal seen
            self.notes.extend(self.inbox())
            fresh = self.notes[seen:]
            seen = len(self.notes)
            return fresh

        return read


@dataclass
class LeadDesk:
    """What the lead's tools work with, and what they leave for the caller.

    `completer_for(role)` gives each role's model as (completer, alias); the
    caller has already routed, budgeted and guarded it.
    """

    completer_for: Callable[[str], tuple[Any, str]]
    max_delegates: int = MAX_DELEGATES
    # Told each new step list, so the owner sees it while the lead works.
    on_todo: Callable[[list[TodoItem]], None] | None = None
    # Told each delegated brief's result, so the run keeps its hand-offs (5e).
    on_result: Callable[[Brief, StepResult], None] | None = None
    # Told each brief's tool calls, numbered; a brief stops when this names a stop.
    on_step: Step | None = None
    halt: Callable[[], str | None] | None = None
    notes: SharedNotes | None = None
    todo: list[TodoItem] = field(default_factory=list)
    results: list[StepResult] = field(default_factory=list)
    proposals: list[dict[str, str]] = field(default_factory=list)


def lead_tools(work: list[ToolSpec], desk: LeadDesk) -> list[ToolSpec]:
    """The lead's tools: the work tools, and todo, delegate, review, propose_team_run."""

    async def todo(arguments: dict[str, Any]) -> ToolResult:
        desk.todo = [TodoItem.model_validate(item) for item in arguments["items"]]
        if desk.on_todo is not None:
            desk.on_todo(desk.todo)
        done = sum(item.status == "done" for item in desk.todo)
        return ToolResult(text=f"Step list updated: {done} of {len(desk.todo)} done.")

    async def delegate(arguments: dict[str, Any]) -> ToolResult:
        briefs = [Brief.model_validate(item) for item in arguments["briefs"]]
        # Each reader from here, so a note sent while any of them works reaches them all.
        inboxes = [desk.notes.reader() if desk.notes else None for _ in briefs]
        results = await _all_or_none(
            _delegated(n, brief, work, desk, inbox)
            for n, (brief, inbox) in enumerate(zip(briefs, inboxes, strict=True), 1)
        )
        desk.results.extend(results)
        if desk.on_result is not None:
            for brief, result in zip(briefs, results, strict=True):
                desk.on_result(brief, result)
        return ToolResult(text="\n\n".join(_shown(n, r) for n, r in enumerate(results, 1)))

    async def review(arguments: dict[str, Any]) -> ToolResult:
        completer, alias = desk.completer_for("critic")
        asked = f"The request:\n{arguments['request']}\n\nThe draft:\n{arguments['draft']}"
        messages = [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": asked},
        ]
        completion = await completer.complete(alias, messages)
        return ToolResult(text=str(completion.content))

    async def propose(arguments: dict[str, Any]) -> ToolResult:
        desk.proposals.append({"objective": arguments["objective"], "why": arguments["why"]})
        return ToolResult(
            text="Proposed to the owner; the owner decides whether to start it. Do not wait: "
            "answer now with what you have, and say that a full team run is proposed and why. "
            "The owner starts it with the button the office shows with your answer; nothing "
            "has started yet. A build is planned by the team first and only then waits for "
            "the owner's Start building, so do not say Start building is there to press now."
        )

    briefs_model = create_model(
        "Briefs",
        briefs=(list[Brief], Field(min_length=1, max_length=desk.max_delegates)),
    )
    return [
        *work,
        ToolSpec(
            "todo",
            "Record and update a short step list for work with several steps, so the owner "
            "sees progress. Send the whole list each time. Skip it for simple one-step "
            "requests.",
            todo,
            Todo,
        ),
        ToolSpec(
            "delegate",
            f"Hand focused, independent briefs to researchers or strategists, up to "
            f"{desk.max_delegates} at once; they work at the same time and each returns a "
            "summary, findings and sources. Write each brief so it stands alone: its objective, "
            "the output you want, where to look, and what is out of scope, so that no two "
            "overlap. A brief that did not finish says so. Do not delegate what one search or "
            "one page read answers.",
            delegate,
            briefs_model,
        ),
        ToolSpec(
            "review",
            "Ask the critic to check a draft answer for unsupported claims, gaps and risks "
            "before you give it. Do not use it for short factual answers.",
            review,
            Review,
        ),
        ToolSpec(
            "propose_team_run",
            "Propose the office's full team (planning, research waves, critique) for this "
            "request; the owner decides whether to start it. Use it only for large "
            "deliverables that need many sources or several days of work, or for building "
            "software, which only the full team does.",
            propose,
            TeamRun,
        ),
    ]


async def _all_or_none(briefs: Iterable[Coroutine[Any, Any, StepResult]]) -> list[StepResult]:
    """Every brief's result; when one raises (a spent budget, an owner question), the
    others are cancelled, so nothing goes on spending for a run that has stopped."""
    tasks = [asyncio.create_task(brief) for brief in briefs]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _delegated(
    n: int,
    brief: Brief,
    work: list[ToolSpec],
    desk: LeadDesk,
    inbox: Callable[[], list[str]] | None = None,
) -> StepResult:
    """One brief, as its own loop with the work tools and a fresh context."""
    completer, alias = desk.completer_for(brief.role)
    messages = [
        {"role": "system", "content": ROLE_PROMPTS[brief.role]},
        {"role": "user", "content": _asked_of(brief)},
    ]

    async def step(event: dict[str, Any]) -> None:
        if desk.on_step is not None and event.get("kind") == "tool_call":
            await desk.on_step({**event, "brief": n})

    result = await run_lead(
        completer,
        alias,
        messages,
        ToolPipeline(work),
        max_steps=BRIEF_STEPS,
        on_step=step,
        inbox=inbox,
        halt=desk.halt,
    )
    if result.asked is not None:
        raise result.asked  # the owner's to answer: the lead stops for it, as for its own call
    return _result_of(result)


def _asked_of(brief: Brief) -> str:
    parts = [f"Your brief:\n{brief.brief}"]
    for title, text in (
        ("Give back", brief.output),
        ("Look first at", brief.sources),
        ("Out of scope", brief.boundaries),
    ):
        if text.strip():
            parts.append(f"{title}:\n{text.strip()}")
    return "\n\n".join(parts)


def _result_of(result: LeadResult) -> StepResult:
    """The brief's result (its text as the summary when it is not JSON), how it ended,
    and only the addresses its own tools returned."""
    try:
        said = StepResult.model_validate(json.loads(strip_code_fence(result.answer)))
    except (ValueError, ValidationError):
        said = StepResult(summary=result.answer.strip())
    [summary, *findings], sources, unreturned = keep_returned(
        [said.summary, *said.findings], said.sources, result.messages
    )
    status = ENDED[result.stopped]
    return StepResult(
        summary=summary,
        findings=findings,
        sources=sources,
        status=status,
        reason="" if status == "done" else result.reason,
        unreturned=unreturned,
    )


def _shown(n: int, result: StepResult) -> str:
    lines = [f"Brief {n}: {result.summary}"]
    if result.status != "done":
        lines = [
            f"Brief {n} did not finish ({result.status}: {result.reason}). "
            f"Do not fill the gap from memory. What it had: {result.summary or 'nothing'}"
        ]
    lines += [f"- {finding}" for finding in result.findings]
    if result.sources:
        lines.append("Sources: " + ", ".join(result.sources))
    if result.unreturned:
        lines.append(
            f"{len(result.unreturned)} addresses it reported were taken out: no tool returned "
            "them. Do not cite what rested only on them without looking it up."
        )
    return "\n".join(lines)
