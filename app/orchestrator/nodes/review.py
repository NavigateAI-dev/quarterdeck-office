"""Review: the critic, and where the pipeline goes after it."""

from collections.abc import Callable
from typing import Any, cast

from app.orchestrator.graph import (
    STOP_NODE,
    _invoke,
    asked_of,
    building,
    judged_repeat,
    plan_of,
    upstream,
)
from app.orchestrator.nodes.context import NodeContext, owner, raised, say, stepped, unresolved
from app.orchestrator.routing import (
    NOT_CONVERGING,
    STOP,
    route_after_critique,
    stalled,
    stop_status,
)
from app.orchestrator.state import AnalysisState, Node


async def critique(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    # The plan carries the acceptance criteria the work is judged against,
    # so a critic without it judges against its own implicit standard.
    inputs = (
        upstream(state, "plan", "research", "strategy")
        + raised(ctx)
        + unresolved(ctx, state)
        + owner(ctx, "critique")
    )
    writes = any(step.kind == "write" for step in plan_of(state).steps)
    if ctx.research_only or (stepped(ctx, state) and not writes):
        inputs += (
            "\n\nThis task has no strategy step: judge the research report itself "
            "against the plan. Use revision_target research for any revision."
        )
    unreachable = list(state.get("unreachable_criteria") or []) + list(
        plan_of(state).unreachable_evidence
    )
    if unreachable:
        listed = "\n".join(f"- {item}" for item in unreachable)
        inputs += (
            "\n\nThis evidence, or these criteria, cannot be reached with what the "
            f"office has:\n{listed}\nReport them as unknowns. "
            "Do not request revision solely because they are unmet."
        )
    produced = await _invoke(ctx.agents.critic, asked_of(state), state, inputs)
    waves = int(state.get("waves") or 0) or None
    if str(produced.get("verdict")) in ("REVISE", "BLOCK"):
        towards = "writer" if produced.get("revision_target") == "strategy" else "researcher"
        for issue in [str(i) for i in produced.get("critical_issues") or []][:4]:
            say(ctx, "challenge", "critic", issue, to_role=towards, phase="review", wave=waves)
    else:
        said = str(produced.get("for_owner") or "").strip() or "The work passes review."
        say(ctx, "note", "critic", said, phase="review", wave=waves)
    previous = cast(dict[str, Any] | None, dict(state).get("last_critique"))
    update: dict[str, Any] = {"critique": produced, "last_critique": produced}
    # Only a critique asking for another revision can mean the loop is not
    # converging. A PASS still lists the gaps the evidence leaves open, and
    # those naturally match the earlier concerns; judging them as repeats
    # overruled the critic and stopped work it had just approved.
    if str(produced.get("verdict")) != "REVISE":
        update["convergence"] = {"method": "not_needed"}
        return update
    stalled_now, how = await judged_repeat(ctx.judge, previous, produced)
    update["convergence"] = how
    if stalled_now:
        update["stopped_reason"] = NOT_CONVERGING
    return update


def decide(ctx: NodeContext, state: AnalysisState) -> str:
    if stalled(state):
        return STOP_NODE
    route = route_after_critique(state, ctx.max_revisions)
    if building(state) and route in (Node.RESEARCH, Node.STRATEGY):
        return Node.REPLAN  # A build is changed by changing its plan.
    if stepped(ctx, state) and route in (Node.RESEARCH, Node.STRATEGY):
        return Node.REPLAN  # A revision is targeted steps, not a whole rerun.
    if ctx.research_only and route == Node.STRATEGY:
        return Node.RESEARCH  # There is no strategy to revise.
    return STOP_NODE if route == STOP else route


def stopped(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    status, reason = stop_status(state, ctx.max_revisions)
    return {"status": status, "stopped_reason": reason}


def continue_or_stop(ctx: NodeContext, following: str) -> Callable[[AnalysisState], str]:
    """Stop a revision that has been found not to be making progress."""

    def choose(state: AnalysisState) -> str:
        return STOP_NODE if stalled(state) else following

    return choose
