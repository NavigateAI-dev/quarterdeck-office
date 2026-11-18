"""What every node of the analysis pipeline shares.

`NodeContext` replaces the 15-parameter closure `build_graph` used to be:
nodes are plain functions taking it first. The helpers here are the team
room, owner notes, the advisor and the small predicates nodes route on.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agent_checks import check_sources
from app.agents.base import AgentOutputError
from app.agents.researcher import normalize_url
from app.agents.teammate import TeammateAgent
from app.judgments import Judge
from app.orchestrator.engineering import EngineeringExecutor
from app.orchestrator.graph import (
    ANALYSIS_WORKFLOW,
    DONE,
    GATHERS,
    PENDING,
    POST_CHARS,
    ROOM_KINDS,
    ROOM_POSTS,
    AdviceMemory,
    AgentSet,
    ApprovalGate,
    TeamBoard,
    _invoke,
    advice_key,
    advisor_view,
    asked_of,
    logger,
    plan_of,
    room_line,
    room_text,
)
from app.orchestrator.state import AnalysisState
from app.schemas import PlanStep


@dataclass(frozen=True)
class NodeContext:
    """Everything a node may use besides the run state. Built once per graph."""

    agents: AgentSet
    max_revisions: int
    research_only: bool
    checkpointer: BaseCheckpointSaver[Any] | None = None
    gate: ApprovalGate | None = None
    executor: EngineeringExecutor | None = None
    judge: Judge | None = None
    guidance: Callable[[str], list[str]] | None = None
    max_parallel: int = 4
    workflow: str = ANALYSIS_WORKFLOW
    advice_memory: "AdviceMemory | None" = None
    board: "TeamBoard | None" = None
    team: bool = False
    teammate: Callable[[Any, str], Any] | None = None
    late_notes: Callable[[str], list[str]] | None = None
    skipped: Callable[[], set[str]] | None = None
    # Steps the owner asked to be done next, the latest first (13d).
    prioritized: Callable[[], list[str]] | None = None


def owner(ctx: NodeContext, node: str) -> str:
    """The owner's instructions, read fresh at the start of every step."""
    items = ctx.guidance(node) if ctx.guidance is not None else []
    if not items:
        return ""
    listed = "\n".join(f"- {item}" for item in items)
    return f"\n\nOwner instructions (apply them; they override earlier direction):\n{listed}"


def say(
    ctx: NodeContext,
    kind: str,
    author_role: str,
    text: str,
    step_id: str | None = None,
    to_role: str | None = None,
    to_step: str | None = None,
    *,
    phase: str | None = None,
    wave: int | None = None,
    reply_to: str | None = None,
) -> None:
    """Post to the task's room. The room never stops the work.

    Each post says the stage it was said in (planning, a wave's work, the
    huddle after it, review, the answer) and the post it answers, if any, so
    the room reads as a conversation."""
    if ctx.board is None or not str(text or "").strip():
        return
    text = " ".join(str(text).split())[:POST_CHARS]
    key = hashlib.sha256(
        f"{kind}|{author_role}|{step_id}|{to_role}|{to_step}|{text}|{reply_to}".encode()
    ).hexdigest()[:32]
    try:
        ctx.board.post(
            {
                "key": key,
                "kind": kind,
                "author_role": author_role,
                "step_id": step_id,
                "to_role": to_role,
                "to_step": to_step,
                "text": text,
                "phase": phase,
                "wave": wave,
                "reply_to": reply_to,
            }
        )
    except Exception:  # noqa: BLE001 - a room that cannot be written to is not a failed task
        logger.warning('{"event":"team_post_failed","kind":"%s"}', kind)


def heard(
    ctx: NodeContext, kinds: tuple[str, ...] = ROOM_KINDS, limit: int = ROOM_POSTS
) -> list[dict[str, Any]]:
    """The room's recent posts of these kinds, or none when the team is not on."""
    if not ctx.team or ctx.board is None:
        return []
    try:
        posts = [p for p in ctx.board.read() if p.get("kind") in kinds]
    except Exception:  # noqa: BLE001 - an unreadable room is an empty one
        return []
    return posts[-limit:]


def room_for(ctx: NodeContext, step_id: str) -> str:
    """What a step reads before it writes: its peers' posts, not its own."""
    posts = [
        p
        for p in heard(ctx, limit=ROOM_POSTS * 3)
        if not (p.get("step_id") == step_id and p.get("author_role") in ("researcher", "writer"))
    ][-ROOM_POSTS:]
    if not posts:
        return ""
    return (
        "Your teammates have posted in the task's room while you worked. Use what "
        "helps: do not repeat ground a peer has covered, flag a contradiction "
        "rather than making one, and cite only sources you read yourself. A post "
        f"addressed to you (→ {step_id}) is yours to act on.\n"
        + room_text([room_line(p) for p in posts])
    )


def late_for(ctx: NodeContext, step_id: str, node: str) -> Callable[[], str]:
    """What reaches a step while it works: teammates' posts and notes
    the owner sent after it began, read just before it writes."""

    def late() -> str:
        parts = [room_for(ctx, step_id)] if ctx.team and ctx.board is not None else []
        fresh = ctx.late_notes(node) if ctx.late_notes is not None else []
        if fresh:
            listed = "\n".join(f"- {item}" for item in fresh)
            parts.append(
                "The owner just sent (apply it; it overrides earlier direction):\n" + listed
            )
        return "\n\n".join(part for part in parts if part)

    return late


def raised(ctx: NodeContext) -> str:
    """What the room raised for the plan and the review to weigh."""
    posts = heard(ctx, ("challenge", "need", "offer", "doubt"))
    if not posts:
        return ""
    return "\n\nRaised in the task's room during the work:\n" + room_text(
        [room_line(p) for p in posts]
    )


def make_teammate(ctx: NodeContext, agent: Any, role: str) -> Any:
    if ctx.teammate is not None:
        return ctx.teammate(agent, role)

    return TeammateAgent(agent, role)


def unresolved(ctx: NodeContext, state: AnalysisState) -> str:
    """What the team raised and could not settle, for the review and the answer."""
    left = [c for c in state.get("challenges") or [] if c.get("outcome") in ("limit", "pending")]
    if not left:
        return ""
    listed = "\n".join(f"- [{c.get('by')} → {c.get('to_step')}] {c.get('text')}" for c in left)
    return "\n\nChallenges the team raised and could not resolve (state them as limits):\n" + listed


def advised(ctx: NodeContext, state: AnalysisState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("advice") or []) + [entry]


async def consult(
    ctx: NodeContext, state: AnalysisState, moment: str, plan_now: dict[str, Any]
) -> dict[str, Any]:
    """Ask the advisor about the course, at one of its three moments.

    It never stops the task: an advisor that fails, or would pass the
    task's budget, is skipped and the record says so.
    """
    if ctx.agents.advisor is None:
        raise RuntimeError("consulted without an advisor")
    key = advice_key(state, moment, plan_now)
    if ctx.advice_memory is not None:
        for kept in reversed(ctx.advice_memory.recall()):
            if kept.get("key") == key:
                return kept
    entry: dict[str, Any] = {
        "moment": moment,
        "key": key,
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        produced = await _invoke(
            ctx.agents.advisor, asked_of(state), state, advisor_view(state, moment, plan_now)
        )
        entry |= produced
    except AgentOutputError as failed:
        skipped = str(failed.code).startswith("budget_exhausted")
        entry |= {
            "verdict": "skipped" if skipped else "unavailable",
            "reason": str(failed.code),
            "advice": [],
            "watch_for": [],
        }
    if ctx.advice_memory is not None:
        ctx.advice_memory.keep(advised(ctx, state, entry))
    said = [str(item) for item in entry.get("advice") or []][:3]
    watch = [str(item) for item in entry.get("watch_for") or []][:2]
    verdict = str(entry.get("verdict") or "").replace("_", " ")
    say(
        ctx,
        "advice",
        "advisor",
        f"{verdict.capitalize()}. "
        + "; ".join(said)
        + (f" Watch for: {'; '.join(watch)}." if watch else ""),
        to_role="chief",
        phase="answer" if moment == "before_done" else "plan",
        wave=int(state.get("waves") or 0) or None,
    )
    return entry


def stepped(ctx: NodeContext, state: AnalysisState) -> bool:
    return bool(state.get("plan")) and bool(plan_of(state).steps)


def ready(ctx: NodeContext, state: AnalysisState) -> list[PlanStep]:
    status = state.get("step_status") or {}
    return [
        step
        for step in plan_of(state).steps
        if status.get(step.id, PENDING) == PENDING
        and all(status.get(needed) == DONE for needed in step.depends_on)
    ]


def with_gaps(ctx: NodeContext, state: AnalysisState) -> list[PlanStep]:
    """Write steps whose inputs have all finished, some with nothing found:
    once research is spent, the answer is written with what there is and
    says what is not established, rather than not written at all."""
    status = state.get("step_status") or {}
    return [
        step
        for step in plan_of(state).steps
        if step.kind == "write"
        and status.get(step.id, PENDING) == PENDING
        and all(status.get(needed, PENDING) != PENDING for needed in step.depends_on)
    ]


def only_research(ctx: NodeContext, produced: dict[str, Any]) -> dict[str, Any]:
    """A researcher's bot writes nothing: its plans keep research steps only."""
    steps = [step for step in produced.get("steps") or [] if step.get("kind") in GATHERS]
    kept = {step["id"] for step in steps}
    for step in steps:
        step["depends_on"] = [needed for needed in step.get("depends_on") or [] if needed in kept]
    return {**produced, "steps": steps}


async def evidenced(ctx: NodeContext, report: dict[str, Any]) -> dict[str, Any]:
    """The research with its local evidence and source checks, as every
    report is handed on: from one pass, parallel questions or plan steps."""
    cited = {normalize_url(str(source.get("url"))) for source in report.get("sources") or []}
    supplied = getattr(ctx.agents.researcher, "last_local", {})
    checked: dict[str, Any] = {
        "research": report,
        "local_evidence": [supplied[url] for url in sorted(cited) if url in supplied],
    }
    if ctx.judge is not None:
        checked["research"], checked["source_checks"] = await check_sources(
            ctx.judge, report, getattr(ctx.agents.researcher, "last_evidence", {})
        )
    return checked
