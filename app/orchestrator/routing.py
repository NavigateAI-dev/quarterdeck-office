from app.orchestrator.state import AnalysisState, Node, RunStatus
from app.schemas import Critique, CritiqueVerdict, RevisionTarget

STOP = "__stop__"

TARGET_NODE = {
    RevisionTarget.RESEARCH: Node.RESEARCH,
    RevisionTarget.STRATEGY: Node.STRATEGY,
}


def revision_budget(state: AnalysisState, max_revisions: int) -> int:
    """How many revisions this cycle may make.

    Work started from a discussion has no automatic revisions (max 0): the
    critic alone cannot spend more. Once the owner has reviewed it, their own
    Revise is their consent and runs once; a further REVISE from the critic
    stops for them again. Found 6 October: the same zero blocked the owner's
    revision too, so every review choice returned to the same stop.
    """
    if state.get("human_cycles", 0):
        return max(max_revisions, 1)
    return max_revisions


def route_after_critique(state: AnalysisState, max_revisions: int) -> str:
    """Select the node that follows a critique.

    PASS advances to synthesis. BLOCK stops without synthesis. REVISE returns to
    its validated target while the revision budget allows; an exhausted budget
    stops for human review rather than looping.
    """
    critique = Critique.model_validate(state["critique"])
    if critique.verdict is CritiqueVerdict.PASS:
        return Node.SYNTHESIS
    if critique.verdict is CritiqueVerdict.BLOCK:
        return STOP
    if state.get("revision_count", 0) >= revision_budget(state, max_revisions):
        return STOP
    if critique.revision_target is None:  # pragma: no cover - schema enforces this
        raise ValueError("A REVISE verdict must set revision_target")
    return TARGET_NODE[critique.revision_target]


# Kept so runs stopped for it before an earlier change still read as stopped.
UNREACHABLE = "evidence_not_reachable"
# The plan cannot go on until the owner answers its questions.
NEEDS_OWNER = "needs_owner_answers"
NO_PROGRESS = "revision_made_no_change"
NOT_CONVERGING = "revision_not_converging"
# No research could run: the model providers were down or refusing.
PROVIDERS_DOWN = "providers_unavailable"
STALLED = (NO_PROGRESS, NOT_CONVERGING, UNREACHABLE, NEEDS_OWNER, PROVIDERS_DOWN)


def stalled(state: AnalysisState) -> bool:
    """Whether the loop has already been found not to be making progress."""
    return state.get("stopped_reason") in STALLED


def stop_status(state: AnalysisState, max_revisions: int) -> tuple[str, str]:
    """Return the terminal status and reason for a stopped run."""
    reason = state.get("stopped_reason")
    if reason in STALLED:
        return RunStatus.NEEDS_HUMAN_REVIEW, str(reason)
    critique = Critique.model_validate(state["critique"])
    if critique.verdict is CritiqueVerdict.BLOCK:
        return RunStatus.BLOCKED, "critic_blocked"
    if state.get("revision_count", 0) >= revision_budget(state, max_revisions):
        return RunStatus.NEEDS_HUMAN_REVIEW, "revision_limit_reached"
    raise ValueError("stop_status called for a non-terminal critique")
