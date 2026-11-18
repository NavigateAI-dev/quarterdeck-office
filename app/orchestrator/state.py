from enum import StrEnum
from typing import Any, TypedDict


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    FAILED = "FAILED"
    # Owner-controlled: paused at a step boundary, or cancelled for good.
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class Node(StrEnum):
    PLAN = "plan"
    RESEARCH = "research"
    STRATEGY = "strategy"
    CRITIQUE = "critique"
    SYNTHESIS = "synthesis"
    APPROVAL = "approval"
    ENGINEERING = "engineering"
    # A step-planned task: a wave of ready steps, and the Chief replanning.
    WORK = "work"
    REPLAN = "replan"


class AnalysisState(TypedDict, total=False):
    """Graph state.

    Agent outputs are stored as plain JSON-compatible dictionaries rather than
    model instances, so a checkpoint round trip cannot depend on model
    reconstruction. Readers validate with the owning schema.
    """

    objective: str
    context: dict[str, Any]
    plan: dict[str, Any] | None
    research: dict[str, Any] | None
    strategy: dict[str, Any] | None
    critique: dict[str, Any] | None
    synthesis: dict[str, Any] | None
    revision_count: int
    # The previous critique, kept through a revision that clears `critique`,
    # so a loop that keeps raising the same concerns can be noticed.
    last_critique: dict[str, Any] | None
    human_cycles: int
    approval_id: str | None
    scope_digest: str | None
    decision: str | None
    engineering: dict[str, Any] | None
    final_report: dict[str, Any] | None
    # Criteria judged unreachable from public sources, and how the last
    # convergence decision was made. Both are evidence, not instructions.
    unreachable_criteria: list[str]
    # A step-planned task: where each step stands (pending, done, empty,
    # failed), what each produced, and the bounds on replanning.
    step_status: dict[str, str]
    step_results: dict[str, dict[str, Any]]
    # Whether each step's skill was applied, or why not.
    step_skills: dict[str, str]
    replans: int
    waves: int
    stalls: int
    # Written with what there is after research failed: research is over.
    gaps_written: bool
    source_checks: list[dict[str, Any]]
    # Parallel research subtasks of the latest research pass: question + report.
    research_subtasks: list[dict[str, Any]]
    # Provenance (source, path, sha256, chunk) of local passages the research cited.
    local_evidence: list[dict[str, Any]]
    unverified_claims: list[str]
    claim_check: str | None
    convergence: dict[str, Any] | None
    # The advisor's consultations: moment, verdict, advice, when.
    advice: list[dict[str, Any]]
    # Huddles held after waves, bounded per run.
    huddles: int
    # The room's challenges and the owner's posts to a step, and what came of
    # each: answered, a stated limit, or waiting for its step.
    challenges: list[dict[str, Any]]
    # Posts already acted on, reruns made and offers taken.
    handled: list[str]
    reruns: int
    offers: int
    status: str
    stopped_reason: str | None
    # The office's project folder a build works in, and the version it made.
    build_folder: str | None
    build_commit: str | None
    # A step that cannot get on, waiting for the owner's hint, skip or finish.
    stuck: dict[str, Any] | None
    # Which provider errors stopped research that could not run at all.
    provider_failure: dict[str, Any]
    # The owner asked for the answer from the work done so far: no more
    # research or replanning, one last writing pass.
    finishing: bool
    # Why the finished answer was made from the notes, when it was.
    written_from_notes: str
    failed_node: str | None


def initial_state(objective: str, context: dict[str, Any]) -> AnalysisState:
    return AnalysisState(
        objective=objective,
        context=context,
        plan=None,
        research=None,
        strategy=None,
        critique=None,
        synthesis=None,
        revision_count=0,
        last_critique=None,
        human_cycles=0,
        approval_id=None,
        scope_digest=None,
        decision=None,
        engineering=None,
        final_report=None,
        unreachable_criteria=[],
        source_checks=[],
        research_subtasks=[],
        local_evidence=[],
        unverified_claims=[],
        claim_check=None,
        convergence=None,
        status=RunStatus.RUNNING,
        stopped_reason=None,
        failed_node=None,
    )
