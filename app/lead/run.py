"""A task run by the lead, in the worker.

The `lead` workflow beside the full team's: the same claim and lease, the same
budget, policies, model router and backups on every role (the worker wraps
them before this runs), but one lead agent sizing the work instead of the fixed
plan → waves → critique → Chief pipeline.

How it ends, as a task does today:
- an answer → the run completes, its answer stored where the task page reads it;
- the owner's limit → it completes with the best answer so far, and says so;
- an owner question → it pauses with the question, like any task;
- a failed provider → the run fails with the code.

A resumed lead task starts its loop again, told the step list and the finished
briefs of its earlier stretch, so it does not pay for those again.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from app.lead.prompt import Section
from app.lead.service import LeadReply, LeadRequest, answer
from app.orchestrator.dispatcher import HEARTBEAT_SECONDS, RunNotClaimed
from app.orchestrator.graph import AgentSet, RunFailure
from app.orchestrator.runner import Dispatch
from app.orchestrator.state import RunStatus
from app.tools.pipeline import ToolSpec

NODE = "lead"
# A delegated brief's typed result, kept as the run's event.
STEP_RESULT = "step_result"
# How many of them a resumed task reads back.
EARLIER_BRIEFS = 16
LEAD_CHARS = 800
# The task page's limits: points per block, blocks per answer.
POINTS = 6
BLOCKS = 20
# Context the task was given, as the lead's context sections, in this order.
CONTEXT = (
    ("project_name", "Project"),
    ("background", "Background"),
    ("objectives", "Objectives"),
    ("decisions", "Decisions made"),
    ("prior_outcomes", "Earlier outcomes"),
    ("lessons", "Lessons"),
    ("local_sources", "The owner's documents"),
    ("attached", "Attached to this task"),
    ("serves", "The objective this serves"),
    ("continues", "What an earlier task found"),
    ("discussion", "The discussion it came from"),
    ("dossiers", "What the office knows about these subjects"),
    ("related", "Related work"),
    ("schedule", "The owner's next days"),
    ("wall", "The owner's wall"),
)


async def execute_lead_run(
    dispatch: Dispatch,
    repository: Any,
    agents: AgentSet,
    objective: str,
    context: dict[str, Any],
    work: list[ToolSpec],
    worker_id: str = "",
    doctrine: str = "",
    resume: bool = False,
) -> dict[str, Any]:
    """Claim, run and record one lead task."""
    if not repository.claim(dispatch.run_id, worker_id):
        raise RunNotClaimed(str(dispatch.run_id))
    run_id = dispatch.run_id
    repository.add_event(run_id, "run_resumed" if resume else "run_started")

    async def step(event: dict[str, Any]) -> None:
        repository.add_event(run_id, "lead_step", event)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            repository.renew_lease(run_id, worker_id)

    earlier, returned = _earlier_stretch(repository, run_id) if resume else ([], [])
    beating = asyncio.create_task(heartbeat()) if worker_id else None
    try:
        reply = await answer(
            LeadRequest(
                request=objective,
                completer_for=lambda role: _model_of(agents, role),
                work=work,
                now=datetime.now(UTC),
                doctrine=doctrine,
                sections=sections_of(context) + earlier,
                returned_before=returned,
                on_step=step,
                # What the owner says while it works reaches it before the next
                # call (steer); a pause or cancel stops it there.
                inbox=lambda: list(repository.take_new_instructions(run_id, NODE)),
                halt=lambda: str((repository.pending_stop(run_id) or {}).get("kind") or "") or None,
                # Its step list on the run's record, for the thread's work strip.
                on_todo=lambda items: repository.merge_state(
                    run_id, {"lead_todo": [item.model_dump() for item in items]}
                ),
                # Each hand-off kept with the run, typed, never a transcript.
                on_result=lambda brief, result: repository.add_event(
                    run_id,
                    STEP_RESULT,
                    {
                        "role": brief.role,
                        "brief": brief.brief,
                        "described": bool(brief.output and brief.sources and brief.boundaries),
                        **result.model_dump(),
                    },
                ),
            )
        )
    finally:
        if beating is not None:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating
    for proposal in reply.proposals:
        repository.add_event(run_id, "team_run_proposed", proposal)
    if reply.asked is not None:
        repository.stop(run_id, RunStatus.PAUSED)
        repository.add_event(
            run_id,
            "policy_ask",
            {
                "policy": reply.asked.decision.policy,
                "reason": reply.asked.decision.reason,
                "record_id": reply.asked.record_id,
            },
        )
        return {"status": RunStatus.PAUSED}
    if reply.stopped == "halted":
        status = RunStatus.CANCELLED if reply.reason == "cancel" else RunStatus.PAUSED
        repository.apply_stop_controls(run_id)
        repository.stop(run_id, status)
        return {"status": status}
    if reply.stopped == "error":
        repository.fail(run_id, NODE, reply.reason)
        repository.add_event(run_id, "run_failed", {"node": NODE, "code": reply.reason})
        raise RunFailure(NODE, reply.reason)
    synthesis = synthesis_of(reply)
    repository.record_stage(dispatch, "synthesis", synthesis)
    state = {
        "status": RunStatus.COMPLETED,
        "synthesis": synthesis,
        # Kept on the finished record too: the work card reads it.
        "lead_todo": [item.model_dump() for item in reply.todo],
        "lead": {
            "stopped": reply.stopped,
            "reason": reply.reason,
            "calls": reply.calls,
            "cited": reply.citations.cited,
            "unresolved": reply.citations.unresolved,
            "todo": [item.model_dump() for item in reply.todo],
            "results": [result.model_dump() for result in reply.results],
            "proposals": reply.proposals,
        },
    }
    repository.finish(run_id, state)
    repository.expire_stop_controls(run_id)
    repository.add_event(run_id, "run_finished", {"status": str(RunStatus.COMPLETED)})
    return state


def _model_of(agents: AgentSet, role: str) -> tuple[Any, str]:
    """The role's model as the worker built it: routed, backed, guarded, budgeted."""
    agent = getattr(agents, role, None) or agents.chief
    return agent.gateway, str(agent.model_alias)


def _earlier_stretch(repository: Any, run_id: Any) -> tuple[list[Section], list[str]]:
    """What a resumed task already did: its step list, finished briefs and their sources."""
    todo = repository.live_state(run_id).get("lead_todo") or []
    finished = [
        result
        for result in repository.recent_events(run_id, STEP_RESULT, EARLIER_BRIEFS)
        if result.get("status") == "done"
    ]
    sections = []
    if todo:
        steps = "\n".join(f"- [{item.get('status')}] {item.get('text')}" for item in todo)
        sections.append(Section("Your step list before this task stopped", steps))
    if finished:
        told = "\n\n".join(
            "\n".join(
                [
                    f"Brief: {result.get('brief')}",
                    f"Result: {result.get('summary')}",
                    *(f"- {finding}" for finding in result.get("findings") or []),
                    *(f"Source: {source}" for source in result.get("sources") or []),
                ]
            )
            for result in reversed(finished)
        )
        sections.append(Section("Briefs already finished: do not repeat them", told))
    return sections, [source for result in finished for source in result.get("sources") or []]


def sections_of(context: dict[str, Any]) -> list[Section]:
    """The task's context as the lead's budgeted sections."""
    found = []
    for key, title in CONTEXT:
        value = context.get(key)
        if isinstance(value, list):
            value = "\n".join(f"- {item}" for item in value if str(item).strip())
        if isinstance(value, dict):
            value = "\n".join(f"- {k}: {v}" for k, v in value.items())
        if value and str(value).strip():
            found.append(Section(title, str(value)))
    return found


def synthesis_of(reply: LeadReply) -> dict[str, Any]:
    """The lead's answer in the shape the task page reads."""
    parts = [part.strip() for part in reply.text.split("\n\n") if part.strip()]
    lead = (parts[0] if parts else "No answer was written.")[:LEAD_CHARS]
    findings = [finding for result in reply.results for finding in result.findings]
    told = []
    if reply.stopped == "limit":
        told.append(f"The lead stopped at a limit ({reply.reason}) before it was done.")
    if reply.citations.unresolved:
        told.append(
            "Cited but not returned by any tool this task: " + ", ".join(reply.citations.unresolved)
        )
    open_steps = [item.text for item in reply.todo if item.status != "done"]
    return {
        "lead": lead,
        "recommendation": lead,
        "facts": findings or [lead],
        "deliverable_body": [
            {
                "heading": "Answer" if n == 0 else "Answer (continued)",
                "points": parts[start : start + POINTS],
            }
            for n, start in enumerate(range(1, len(parts), POINTS))
        ][:BLOCKS],
        "next_steps": open_steps[:6],
        "not_checked": " ".join(told)[:1200],
        "proposes_action": False,
    }
