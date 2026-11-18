from enum import StrEnum
from typing import Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceType(StrEnum):
    """Whether a source is the subject's own record or commentary about it."""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class Source(StrictModel):
    title: str = Field(min_length=1)
    # A web address, or a local:// citation of an owner's document passage.
    url: AnyUrl
    # The researcher must classify. A search provider usually cannot tell, and
    # leaving it unclassified makes the evidence impossible to weigh.
    source_type: SourceType
    # Set by the application to the date the source was actually retrieved;
    # any value a model writes here is replaced.
    accessed_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class TaskKind(StrEnum):
    """What kind of deliverable an objective calls for.

    The chain is fixed; the contract the deliverable must satisfy is not. A
    request to compare tools is not a product hypothesis and should not be
    judged as one.
    """

    PRODUCT_PROPOSAL = "product_proposal"
    RESEARCH_SUMMARY = "research_summary"
    # Something made: a website, an app, a script, a tool. Planned as a
    # spec, built by a coding agent once the owner presses Start building.
    BUILD = "build"


# How many steps one plan may hold: enough for a real task, few enough
# that a plan cannot grow into fifty workers for a simple question.
MAX_STEPS = 8


class PlanStep(StrictModel):
    """One step of the Chief's plan, from a fixed catalogue.

    `research` answers one question with the researcher's web, document and
    tool access; `browse` does it in the office's own browser, when the owner
    has it on; `write` produces the deliverable from the steps it depends on.
    """

    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["research", "browse", "write"]
    # What this step must do, as its worker is told: the question, the
    # boundaries, what to return.
    brief: str = Field(min_length=1, max_length=1200)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    # How much work the step deserves; research reads more pages when deeper.
    effort: Literal["light", "standard", "deep"] = "standard"
    # Why a replan added this step, when one did.
    added_because: str = Field(default="", max_length=400)
    # An installed skill this step's worker follows, by its id.
    skill: str = Field(default="", max_length=64)


class ReaderBrief(StrictModel):
    """Who the answer is for and what they will do with it.

    Written before the steps, and every role works from it: plans had become
    audit checklists, critiques audits, and answers evidence logs, because
    nothing said who was reading or what would be good enough for them.
    """

    # Who reads it, in a phrase: "a parent in Lyon choosing an art class".
    reader: str = Field(min_length=1, max_length=300)
    # What they will decide or do with it.
    purpose: str = Field(min_length=1, max_length=400)
    # What good enough looks like to them, in their terms, not as procedure.
    good_enough: list[str] = Field(min_length=1, max_length=4)
    # The language, words and tone they expect.
    voice: str = Field(default="", max_length=200)


class PlanChange(StrictModel):
    change: str = Field(min_length=1, max_length=400)
    why: str = Field(min_length=1, max_length=300)


class PlanReview(StrictModel):
    """A quick check of a draft plan against the reader brief."""

    verdict: Literal["ready", "change"]
    changes: list[PlanChange] = Field(default_factory=list, max_length=3)
    # One plain sentence on the plan, for the room.
    plain: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def changes_for_change(self) -> "PlanReview":
        if self.verdict == "change" and not self.changes:
            raise ValueError("Asking for a change needs the change")
        return self


class CodeFinding(StrictModel):
    path: str = Field(default="", max_length=300)
    issue: str = Field(min_length=1, max_length=400)


class CodeReview(StrictModel):
    """A reviewer's reading of a coding task's diff against what was asked (36c).

    Evidence for the owner, never a verdict on its own: the office's checks
    decide whether the work passed."""

    verdict: Literal["looks_right", "concerns"]
    findings: list[CodeFinding] = Field(default_factory=list, max_length=5)
    # One plain sentence on the change, for the review card.
    plain: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def findings_for_concerns(self) -> "CodeReview":
        if self.verdict == "concerns" and not self.findings:
            raise ValueError("Concerns need the findings")
        return self


class BuildSpec(StrictModel):
    """What will be built, for the owner to read before pressing Start."""

    # What it is, in a sentence the owner would use.
    summary: str = Field(min_length=1, max_length=600)
    # What it does, each a thing the owner can try.
    features: list[str] = Field(min_length=1, max_length=12)
    # How it is made, chosen for the owner: "plain HTML, CSS and JavaScript, no build step".
    stack: str = Field(min_length=1, max_length=300)
    # How the owner runs or opens it.
    how_to_run: str = Field(min_length=1, max_length=600)
    # What the check script proves before it is called done.
    checks: list[str] = Field(min_length=1, max_length=10)


class TaskPlan(StrictModel):
    """The Chief's task contract for one objective (SPEC section 11)."""

    objective: str = Field(min_length=1)
    # Who the answer is for, written first. None in plans made before it.
    reader_brief: ReaderBrief | None = None
    # For a build: what will be made. Required then, empty otherwise.
    build_spec: BuildSpec | None = None
    task_kind: TaskKind
    research_question: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    expected_artifacts: list[str] = Field(min_length=1)
    # Whether all the evidence the criteria would like can be reached. False
    # names what cannot, in unreachable_evidence: a limit the answer states,
    # never a reason to stop. Kept for plans recorded before that.
    evidence_reachable: bool = True
    unreachable_evidence: list[str] = Field(default_factory=list)
    # What only the owner can tell: their constraints, what they meant. Asked
    # alongside the work, and before it only when needs_owner_first.
    owner_questions: list[str] = Field(default_factory=list, max_length=3)
    # True only when the question cannot even be understood without the
    # owner's answers. The one thing that stops a plan.
    needs_owner_first: bool = False
    # The work as steps. Empty is the fixed pipeline, as every plan made
    # before steps existed runs.
    steps: list[PlanStep] = Field(default_factory=list)
    # Independent parts of the research question, researched in parallel.
    # Empty or a single item means one research pass, as before.
    subquestions: list[str] = Field(default_factory=list, max_length=4)
    # The subjects' own pages likely to hold the evidence. Fetched directly;
    # only pages that are actually retrieved become citable.
    official_sources: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def a_build_says_what_it_builds(self) -> "TaskPlan":
        if self.task_kind == TaskKind.BUILD and self.build_spec is None:
            raise ValueError("A build plan must carry its build_spec")
        return self

    @model_validator(mode="after")
    def name_what_cannot_be_reached(self) -> "TaskPlan":
        if not self.evidence_reachable and not self.unreachable_evidence:
            raise ValueError("Unreachable evidence must be named")
        if self.needs_owner_first and not any(q.strip() for q in self.owner_questions):
            raise ValueError("Waiting for the owner needs a question for the owner")
        self.waves()  # A plan the executor could not run is refused here.
        return self

    def waves(self) -> list[list[str]]:
        """The steps in the order they can run: each wave depends only on earlier ones."""
        if len(self.steps) > MAX_STEPS:
            raise ValueError(f"A plan holds at most {MAX_STEPS} steps")
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("A step id is used twice")
        known = set(ids)
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"Step {step.id} depends on unknown steps: {sorted(missing)}")
        done: set[str] = set()
        order: list[list[str]] = []
        remaining = list(self.steps)
        while remaining:
            ready = [step.id for step in remaining if set(step.depends_on) <= done]
            if not ready:
                raise ValueError("The steps depend on each other in a cycle")
            order.append(ready)
            done |= set(ready)
            remaining = [step for step in remaining if step.id not in done]
        return order


class AnswerStatus(StrEnum):
    """Whether a question was actually answered, on the evidence retrieved."""

    ANSWERED = "answered"
    PARTIAL = "partial"
    UNANSWERED = "unanswered"


class QuestionAnswer(StrictModel):
    """One question, and what the evidence says about it.

    A research task that lists its own questions as unknowns has not answered
    them, and should say so per question rather than in a pile of unknowns at
    the end that nobody can map back to what was asked.
    """

    question: str = Field(min_length=1)
    status: AnswerStatus
    # What the evidence supports, or why the question cannot be answered yet.
    answer: str = Field(min_length=1)
    # The sources backing this answer, by the exact URL or local:// address.
    evidence: list[str] = Field(default_factory=list)
    # What would be needed to answer it, when it is not answered.
    missing: list[str] = Field(default_factory=list)


class BlockedSource(StrictModel):
    """A source named in the plan that refused to be read.

    Recorded so a gap is traceable to a refusal rather than presented as though
    nothing exists to be found.
    """

    url: str = Field(min_length=1)
    status: str = Field(min_length=1)


class ResearchReport(StrictModel):
    research_question: str = Field(min_length=1)
    # One entry per question the plan asked, answered or not.
    answers: list[QuestionAnswer] = Field(default_factory=list)
    blocked_sources: list[BlockedSource] = Field(default_factory=list)
    key_findings: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    contradictory_evidence: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    implications: list[str] = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    confidence: str = Field(min_length=1)


class ComparisonTable(StrictModel):
    """A side-by-side comparison: named columns and rows of cells."""

    columns: list[str] = Field(min_length=2, max_length=10)
    rows: list[list[str]] = Field(min_length=1, max_length=40)
    # The source address each row's values come from, one per row.
    row_sources: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def rows_match_columns(self) -> "ComparisonTable":
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Each row must have one cell per column")
        if len(self.row_sources) != len(self.rows):
            raise ValueError("The table must name one source per row")
        return self


class ResearchSummary(StrictModel):
    """The deliverable for a research summary.

    Deliberately has no market or demand fields: an objective that asks what
    exists should not be required to argue that someone would pay for it.
    """

    question: str = Field(min_length=1)
    findings: list[str] = Field(min_length=1)
    comparison: list[str] = Field(default_factory=list)
    trade_offs: list[str] = Field(default_factory=list)
    limits: list[str] = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)
    # When options are compared, the comparison as a table; cells hold the
    # published values and units, or "not documented".
    comparison_table: ComparisonTable | None = None


class StrategyProposal(StrictModel):
    problem: str = Field(min_length=1)
    who_has_it: str = Field(min_length=1)
    frequency_severity: str = Field(min_length=1)
    current_behavior: str = Field(min_length=1)
    willingness_to_change: str = Field(min_length=1)
    supporting_evidence: list[str] = Field(min_length=1)
    smallest_useful_test: str = Field(min_length=1)
    invalidation_condition: str = Field(min_length=1)
    icp: str = Field(min_length=1)
    jtbd: str = Field(min_length=1)
    mvp_scope: list[str] = Field(min_length=1)
    success_metrics: list[str] = Field(min_length=1)


class CritiqueVerdict(StrEnum):
    PASS = "PASS"
    REVISE = "REVISE"
    BLOCK = "BLOCK"


class RevisionTarget(StrEnum):
    RESEARCH = "research"
    STRATEGY = "strategy"


class CritiqueIssue(StrictModel):
    """One thing that matters to the reader, most important first."""

    issue: str = Field(min_length=1, max_length=400)
    why_it_matters: str = Field(min_length=1, max_length=300)
    fix: str = Field(min_length=1, max_length=400)


class Critique(StrictModel):
    verdict: CritiqueVerdict
    revision_target: RevisionTarget | None = None
    # At most three issues, ranked by what they cost the reader.
    issues: list[CritiqueIssue] = Field(default_factory=list, max_length=3)
    # Changes to the wording or order the answer should make, with no new research.
    writing_fixes: list[str] = Field(default_factory=list, max_length=5)
    # One plain sentence for the owner: what is good, what is missing.
    for_owner: str = Field(default="", max_length=400)
    critical_issues: list[str] = Field(default_factory=list)
    weak_assumptions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    required_corrections: list[str] = Field(default_factory=list)
    what_would_change_conclusion: list[str] = Field(default_factory=list)
    confidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_target_for_revision(self) -> "Critique":
        """A REVISE verdict must name where the workflow returns to.

        Without a target the workflow has no defined edge, so an untargeted
        revision is rejected rather than guessed.
        """
        if self.verdict is CritiqueVerdict.REVISE and self.revision_target is None:
            raise ValueError("A REVISE verdict must set revision_target")
        if self.verdict is not CritiqueVerdict.REVISE and self.revision_target is not None:
            raise ValueError("revision_target is only valid for a REVISE verdict")
        if self.issues and not self.critical_issues and self.verdict is not CritiqueVerdict.PASS:
            # What the room, the review and the convergence check read.
            self.critical_issues = [item.issue for item in self.issues]
        return self


class ReactionPost(StrictModel):
    """One thing said in a huddle, from one role's angle."""

    kind: Literal["challenge", "need", "offer", "note"]
    # The step it concerns, when it concerns one.
    to_step: str | None = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=400)
    # The number of the room's post this answers, when it answers one.
    reply_to: int | None = Field(default=None, ge=1)


class Reaction(StrictModel):
    """A role's reaction to a wave's work: at most three short posts, or none."""

    posts: list[ReactionPost] = Field(default_factory=list, max_length=3)


class AdviceVerdict(StrEnum):
    CONTINUE = "continue"
    CHANGE_COURSE = "change_course"


class Advice(StrictModel):
    """The advisor's word on the course of a task. It writes no work."""

    verdict: AdviceVerdict
    # What the Chief should do differently, most important first.
    advice: list[str] = Field(default_factory=list, max_length=6)
    # What to check before trusting the next step.
    watch_for: list[str] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)


class BodyTable(StrictModel):
    """Numbers behind a block: the first column labels or periods, the rest numbers."""

    columns: list[str] = Field(min_length=2, max_length=6)
    rows: list[list[str]] = Field(min_length=1, max_length=24)


class BodyBlock(StrictModel):
    """One headed block of the deliverable the owner asked for.

    A slide, a section of a brief, an item on a checklist: every shape that
    needs its own structure needs the same structure, so there is one field for
    all of them rather than a schema change per shape.
    """

    heading: str = Field(min_length=1, max_length=90)
    points: list[str] = Field(min_length=1, max_length=6)
    # Its numbers, when it has any: the office draws the chart from them.
    table: BodyTable | None = None


class Synthesis(StrictModel):
    # The answer in one to three plain sentences, first thing read.
    lead: str = Field(default="", max_length=800)
    # What the reader can do next, in order.
    next_steps: list[str] = Field(default_factory=list, max_length=6)
    # What could not be checked, said once, briefly, at the end.
    not_checked: str = Field(default="", max_length=1200)
    facts: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    recommendation: str = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    # Whether the owner is asked to authorise something to be done: built,
    # executed, published, spent. Only then is there an approval. None is
    # a synthesis recorded before this was asked, read as it was then.
    proposes_action: bool | None = None
    # What would be done, when something is proposed; empty otherwise.
    proposed_engineering_scope: str = ""
    # Checkable outcomes for the proposed engineering work. They are bound into
    # the approval and handed to the engineering runtime as its contract.
    engineering_acceptance_criteria: list[str] = Field(default_factory=list, max_length=12)
    # The deliverable itself, filled only when the shape the owner asked for
    # says so. Without it a deck is the report's own sections — answer, facts,
    # unknowns — which is a report with page breaks, not a presentation.
    deliverable_body: list[BodyBlock] = Field(default_factory=list, max_length=20)
