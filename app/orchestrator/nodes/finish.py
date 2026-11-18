"""Finishing: the answer, approval, and the approved engineering."""

from typing import Any, cast

from app.agent_checks import unverified_claims
from app.agents.base import AgentOutputError
from app.coder.review import reviewed
from app.judgments import JudgmentUnavailable
from app.orchestrator.graph import (
    BUILD_ANSWER,
    FINISHED_EARLY,
    _invoke,
    advice_for_chief,
    as_build_proposal,
    as_found,
    asked_of,
    build_outcome,
    building,
    context_of,
    coverage,
    final_report,
    left_undone,
    limits_of,
    upstream,
)
from app.orchestrator.nodes.context import NodeContext, advised, consult, owner, say, unresolved
from app.orchestrator.state import AnalysisState, RunStatus
from app.schemas.engineering import EngineeringStatus


async def synthesis(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    if state.get("finishing"):
        return await finish(ctx, state)
    update: dict[str, Any] = {}
    said = ""
    if ctx.agents.advisor is not None:
        # Before the work is declared done (宣布完成前).
        entry = await consult(ctx, state, "before_done", dict(state.get("plan") or {}))
        update["advice"] = advised(ctx, state, entry)
        said = advice_for_chief(entry)
    produced = await _invoke(
        ctx.agents.chief,
        asked_of(state),
        state,
        upstream(state, "plan", "research", "strategy", "critique")
        + limits_of(state)
        + unresolved(ctx, state)
        + said
        + (BUILD_ANSWER if building(state) else "")
        + owner(ctx, "synthesis"),
    )
    if building(state):
        produced = as_build_proposal(state, produced)
    update.update(synthesis=produced, status=RunStatus.COMPLETED)
    say(ctx, "note", "chief", "The answer is written.", phase="answer")
    if ctx.judge is not None:
        # Informs the owner's decision; never blocks it.
        try:
            update["unverified_claims"] = await unverified_claims(
                ctx.judge, produced, cast(dict[str, Any] | None, dict(state).get("research"))
            )
            update["claim_check"] = "typesafe"
        except JudgmentUnavailable:
            update["claim_check"] = "unavailable"
    return update


async def finish(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    """The answer from the work as it stands: one writing pass, or the notes."""
    undone = left_undone(state)
    pending = (
        "\n\nPlanned but not done:\n" + "\n".join(f"- {brief}" for brief in undone)
        if undone
        else ""
    )
    update: dict[str, Any] = {"status": RunStatus.COMPLETED, "finishing": False}
    try:
        produced = await _invoke(
            ctx.agents.chief,
            asked_of(state),
            state,
            upstream(state, "research", "strategy")
            + limits_of(state)
            + pending
            + FINISHED_EARLY
            + owner(ctx, "synthesis"),
        )
        say(
            ctx, "note", "chief", "The answer is written from the work done so far.", phase="answer"
        )
    except AgentOutputError as failed:
        produced = as_found(state)
        update["written_from_notes"] = failed.code
        say(
            ctx,
            "note",
            "chief",
            "The answer is put together from the notes: the last writing pass "
            f"could not run ({failed.code}).",
            phase="answer",
        )
    # Finishing asks for the answer, not for leave to act on it: what the
    # Chief recommends stays in the answer, and nothing waits for approval.
    update["synthesis"] = produced | {
        "proposes_action": False,
        "proposed_engineering_scope": "",
        "engineering_acceptance_criteria": [],
    }
    update["claim_check"] = "skipped"
    return update


def approval(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    """Persist the proposal and stop until a human decides (SPEC 19)."""
    if ctx.gate is None:
        return {"status": RunStatus.COMPLETED}
    synthesis = cast(dict[str, Any], dict(state).get("synthesis") or {})
    flagged = list(state.get("unverified_claims") or [])
    approval_id, digest = ctx.gate.open(
        asked_of(state),
        {**synthesis, "unverified_claims": flagged, "building": building(state)},
    )
    update: dict[str, Any] = {
        "approval_id": approval_id,
        "scope_digest": digest,
        "status": RunStatus.WAITING_FOR_APPROVAL,
    }
    folder = getattr(ctx.gate, "build_folder", None)
    if folder:
        update["build_folder"] = folder
    return update


async def engineering(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    """Act on the recorded decision. Authorization comes from the record."""
    if ctx.gate is None or ctx.executor is None:
        return {"status": RunStatus.COMPLETED}
    approval_id = state.get("approval_id")
    decision = ctx.gate.decision_for(str(approval_id)) if approval_id else None
    if decision is None:
        return {"status": RunStatus.WAITING_FOR_APPROVAL}
    verdict, scope = decision
    if verdict != "APPROVED":
        return {
            "decision": verdict,
            "status": RunStatus.REJECTED,
            "stopped_reason": "rejected_by_owner",
        }
    # The digest recorded when the proposal was shown must still match the
    # scope that was approved, or this is not the work the owner reviewed.
    if state.get("scope_digest") != scope.digest():
        return {
            "decision": verdict,
            "status": RunStatus.NEEDS_HUMAN_REVIEW,
            "stopped_reason": "scope_changed_after_approval",
        }
    if not scope.repository:
        # Nothing was proposed for execution: the brief said approving
        # records the decision, and that is the whole outcome.
        return {
            "decision": verdict,
            "final_report": {**final_report(scope, None), "coverage": coverage(state)},
            "status": RunStatus.COMPLETED,
        }
    # One approval is carried out once. Recovery re-enters here when
    # the process died after the approval was decided; work already done
    # is reported, not repeated, and work whose outcome is unknown (the
    # process died during it) waits for the owner rather than running, and
    # perhaps publishing, a second time.
    begin = getattr(ctx.gate, "begin_execution", None)
    held, earlier = begin(str(approval_id), scope.digest()) if begin else ("new", None)
    if held == "in_flight":
        # The coder on the owner's Mac may still be working on the job handed
        # out before the restart: wait for that one, never hand out another.
        reattach = getattr(ctx.executor, "reattach", None)
        earlier = await reattach(scope) if reattach is not None else None
        if earlier is not None:
            finish = getattr(ctx.gate, "finish_execution", None)
            if finish is not None:
                finish(str(approval_id), earlier)
            held = "finished"
    if held == "in_flight":
        return {
            "decision": verdict,
            "final_report": {**final_report(scope, None), "coverage": coverage(state)},
            "status": RunStatus.NEEDS_HUMAN_REVIEW,
            "stopped_reason": "engineering_outcome_uncertain",
        }
    if held == "finished" and earlier is not None:
        report = earlier
    else:
        report = await ctx.executor.execute(scope, approved=True)
        # A second pair of eyes before the owner merges: evidence, not a verdict.
        review = await reviewed(ctx.agents.code_reviewer, scope, report, context_of(state))
        if review is not None:
            report["review"] = review
        finish = getattr(ctx.gate, "finish_execution", None)
        if finish is not None:
            finish(str(approval_id), report)
    summary = {**final_report(scope, report), "coverage": coverage(state)}
    told = build_outcome(state, report) if building(state) else {}
    if report.get("commit"):
        told["build_commit"] = str(report["commit"])  # This task's version.
    if "runtime:stopped_by_owner" in (report.get("errors") or []):
        # The owner stopped the build: cancelled, nothing kept.
        return {
            "decision": verdict,
            "engineering": report,
            "final_report": summary,
            **told,
            "status": RunStatus.CANCELLED,
            "stopped_reason": "stopped_by_owner",
        }
    if report.get("status") != EngineeringStatus.COMPLETED:
        return {
            "decision": verdict,
            "engineering": report,
            "final_report": summary,
            **told,
            "status": RunStatus.FAILED,
            "stopped_reason": "engineering_failed",
        }
    return {
        "decision": verdict,
        "engineering": report,
        "final_report": summary,
        **told,
        "status": RunStatus.COMPLETED,
    }
