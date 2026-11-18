"""Planning nodes: plan, its review, and replanning."""

from typing import Any, cast

from app.agents.base import AgentOutputError
from app.judgments import JudgmentUnavailable
from app.orchestrator.graph import (
    DONE,
    PENDING,
    PLAN_REVIEWS,
    REPLAN_RULES,
    STOP_NODE,
    UNREACHABLE_PROBABILITY,
    _invoke,
    advice_for_chief,
    asked_of,
    building,
    plan_diff,
    plan_of,
    render,
    short,
    skills_offered,
    upstream,
)
from app.orchestrator.nodes.context import (
    NodeContext,
    advised,
    consult,
    only_research,
    owner,
    raised,
    ready,
    say,
    stepped,
)
from app.orchestrator.routing import NEEDS_OWNER, NO_PROGRESS, stalled
from app.orchestrator.state import AnalysisState, Node
from app.schemas import TaskPlan
from app.schemas.agent_output import PlanReview


async def plan(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    # The work, without the conversation that carried it: a trailing "reply
    # yes to confirm" is an instruction about the request, and planning
    # against it produces a task whose deliverable is an approval.
    produced = await _invoke(
        ctx.agents.planner,
        asked_of(state),
        state,
        owner(ctx, "plan") + skills_offered(ctx.agents.skills),
    )
    update: dict[str, Any] = {"plan": produced}
    contract = TaskPlan.model_validate(produced)
    if ctx.research_only and contract.steps:
        update["plan"] = produced = only_research(ctx, produced)
        contract = TaskPlan.model_validate(produced)
    if contract.steps and not contract.needs_owner_first and ctx.agents.advisor is not None:
        # Before the plan is committed (定计划前). A change of course has the
        # Chief revise the plan once, with the advice in hand.
        entry = await consult(ctx, state, "before_plan", produced)
        update["advice"] = advised(ctx, state, entry)
        if entry.get("verdict") == "change_course":
            try:
                revised = await _invoke(
                    ctx.agents.planner,
                    asked_of(state),
                    state,
                    render("Your plan", produced)
                    + advice_for_chief(entry)
                    + "\n\nRevise the plan where the advice holds up; "
                    + "keep what it does not touch."
                    + skills_offered(ctx.agents.skills),
                )
                if ctx.research_only:
                    revised = only_research(ctx, revised)
                if TaskPlan.model_validate(revised).steps:
                    update["plan"] = produced = revised
                    contract = TaskPlan.model_validate(produced)
            except AgentOutputError as failed:
                if str(failed.code).startswith("budget_exhausted"):
                    raise
                # The first plan stands.
    if contract.steps and not contract.needs_owner_first and ctx.agents.plan_reviewer is not None:
        # Reviewed before any work: challenged, revised, compared,
        # and started once it is clear enough.
        produced, contract = await review_plan(ctx, state, produced, contract)
        update["plan"] = produced
    # Only a question the work cannot be understood without stops it.
    # Evidence out of reach is a limit the answer states: refusing the
    # public half of the work for want of the private half helped no one.
    if contract.steps:
        say(
            ctx,
            "plan",
            "chief",
            f"Plan: {len(contract.steps)} steps. "
            + "; ".join(f"{s.id}: {short(s.brief, 60)}" for s in contract.steps[:8]),
            phase="plan",
        )
        update.update(
            step_status={step.id: PENDING for step in contract.steps},
            step_results={},
            replans=0,
            waves=0,
            stalls=0,
        )
    if contract.needs_owner_first:
        update["stopped_reason"] = NEEDS_OWNER
        return update
    update["unreachable_criteria"] = []
    if ctx.judge is not None:
        try:
            local = list((state.get("context") or {}).get("local_sources") or [])
            reach = await ctx.judge.reachability(contract.acceptance_criteria, local)
        except JudgmentUnavailable:
            reach = []
        unreachable = [
            criterion
            for criterion, p in zip(contract.acceptance_criteria, reach, strict=False)
            if p < UNREACHABLE_PROBABILITY
        ]
        # Told to the critic as what not to demand; it stops nothing.
        update["unreachable_criteria"] = unreachable
    return update


async def review_plan(
    ctx: NodeContext, state: AnalysisState, produced: dict[str, Any], contract: TaskPlan
) -> tuple[dict[str, Any], TaskPlan]:
    """Review the draft plan against its reader brief, revise, compare; at
    most PLAN_REVIEWS rounds, ending early once it is ready or a revision
    changes nothing that matters."""
    reviewer = ctx.agents.plan_reviewer
    assert reviewer is not None  # noqa: S101 - checked by the caller
    for round_number in range(1, PLAN_REVIEWS + 1):
        try:
            verdict = PlanReview.model_validate(
                await _invoke(
                    reviewer,
                    asked_of(state),
                    state,
                    render("The draft plan", produced) + owner(ctx, "plan"),
                )
            )
        except AgentOutputError as failed:
            if str(failed.code).startswith("budget_exhausted"):
                raise
            return produced, contract  # The draft stands.
        if verdict.verdict == "ready" or not verdict.changes:
            say(
                ctx,
                "note",
                "critic",
                f"Plan reviewed: ready. {verdict.plain}".strip(),
                phase="plan",
            )
            return produced, contract
        asked = "\n".join(f"- {c.change} (why: {c.why})" for c in verdict.changes)
        say(
            ctx,
            "challenge",
            "critic",
            f"Plan review {round_number}: " + "; ".join(c.change for c in verdict.changes),
            to_role="chief",
            phase="plan",
        )
        try:
            revised = await _invoke(
                ctx.agents.planner,
                asked_of(state),
                state,
                render("Your draft plan", produced)
                + "\n\nThe plan review asks for these changes:\n"
                + asked
                + "\n\nRevise the plan: apply the changes that serve the reader, keep "
                "the rest as it is, and keep it small."
                + owner(ctx, "plan")
                + skills_offered(ctx.agents.skills),
            )
            if ctx.research_only:
                revised = only_research(ctx, revised)
            candidate = TaskPlan.model_validate(revised)
        except AgentOutputError as failed:
            if str(failed.code).startswith("budget_exhausted"):
                raise
            return produced, contract
        if not candidate.steps:
            return produced, contract
        changed = plan_diff(contract, candidate)
        say(
            ctx,
            "plan",
            "chief",
            f"Plan revised ({changed or 'no change to the steps'}): "
            + "; ".join(f"{s.id}: {short(s.brief, 60)}" for s in candidate.steps[:8]),
            phase="plan",
        )
        produced, contract = revised, candidate
        if not changed:
            break  # Nothing that matters moved: the plan is as clear as it gets.
    return produced, contract


async def replan(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    """The Chief rewrites the steps not yet done, keeping what stands.

    After a step failed or found nothing, or when the critic asked for a
    revision: then the revision is targeted steps, not a whole rerun.
    """
    old = plan_of(state)
    status = dict(state.get("step_status") or {})
    results = dict(state.get("step_results") or {})
    critiqued = cast(dict[str, Any], dict(state).get("critique") or {})
    revising = str(critiqued.get("verdict")) == "REVISE"
    progress = "\n".join(
        f"- {step.id} ({step.kind}, {status.get(step.id, PENDING)}): {step.brief}"
        for step in old.steps
    )
    inputs = (
        upstream(state, "plan", "critique")
        + f"\n\nReplan this task. Where its steps stand:\n{progress}\n"
        + raised(ctx)
        + REPLAN_RULES
        + owner(ctx, "plan")
        + skills_offered(ctx.agents.skills)
    )
    # The waves without progress are still counted after a replan: reset,
    # every empty wave earned another replan, and the task never ended.
    update: dict[str, Any] = {}
    # The same failure again (同一错误重复): a second replan after failed or
    # empty steps, or a second revision asked by the critic.
    repeat = (
        int(state.get("revision_count") or 0) >= 1
        if revising
        else int(state.get("replans") or 0) >= 1
    )
    if repeat and ctx.agents.advisor is not None:
        entry = await consult(ctx, state, "repeated_failure", dict(state.get("plan") or {}))
        update["advice"] = advised(ctx, state, entry)
        inputs += advice_for_chief(entry)
    if revising:
        update.update(critique=None, revision_count=state.get("revision_count", 0) + 1)
    else:
        update["replans"] = int(state.get("replans") or 0) + 1
    try:
        produced = await _invoke(ctx.agents.planner, asked_of(state), state, inputs)
    except AgentOutputError as failed:
        if str(failed.code).startswith("budget_exhausted"):
            raise
        return update  # The plan stands; review sees the gaps.
    if ctx.research_only:
        produced = only_research(ctx, produced)
    new = TaskPlan.model_validate(produced)
    before = {step.id: step for step in old.steps}
    kept: dict[str, str] = {}
    for wave_ids in new.waves():
        for step_id in wave_ids:
            step = next(s for s in new.steps if s.id == step_id)
            was = before.get(step.id)
            unchanged = (
                was is not None
                and status.get(step.id) == DONE
                and was.brief == step.brief
                and was.depends_on == step.depends_on
                and all(kept.get(needed) == DONE for needed in step.depends_on)
            )
            kept[step.id] = DONE if unchanged else PENDING
    update.update(
        plan=produced,
        step_status=kept,
        step_results={
            step_id: results[step_id]
            for step_id in kept
            if kept[step_id] == DONE and step_id in results
        },
    )
    fresh = [step for step in new.steps if kept.get(step.id) == PENDING]
    reason = next((step.added_because for step in fresh if step.added_because), "")
    say(
        ctx,
        "plan",
        "chief",
        f"Replanned: {len(fresh)} steps to do. "
        + "; ".join(f"{s.id}: {short(s.brief, 60)}" for s in fresh[:6])
        + (f". Why: {short(reason, 160)}" if reason else ""),
        phase="plan",
        wave=int(state.get("waves") or 0) or None,
    )
    if revising and not building(state) and not any(value == PENDING for value in kept.values()):
        # Asked to revise, and the new plan changes nothing: no progress.
        update["stopped_reason"] = NO_PROGRESS
    return update


def after_replan(ctx: NodeContext, state: AnalysisState) -> str:
    if stalled(state):
        return STOP_NODE
    if building(state) and not ready(ctx, state):
        return Node.SYNTHESIS  # The changed build, proposed again.
    return Node.WORK if ready(ctx, state) else Node.CRITIQUE


def after_plan(ctx: NodeContext, state: AnalysisState) -> str:
    if stalled(state):
        return STOP_NODE
    if building(state) and not stepped(ctx, state):
        # Nothing to look up first: straight to the proposal.
        return Node.SYNTHESIS
    return Node.WORK if stepped(ctx, state) else Node.RESEARCH
