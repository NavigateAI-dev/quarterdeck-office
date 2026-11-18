import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.coding_names import label_of


class ArtifactRef(BaseModel):
    """Evidence bound into an approval: identity, version, and content digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    type: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    content_digest: str = Field(min_length=64, max_length=64)


class ApprovalScope(BaseModel):
    """The exact, immutable proposal a human is asked to approve.

    Everything an approval authorizes is named here. Nothing outside this
    object is authorized by approving it, and any change to the underlying
    artifacts changes the digest, which invalidates a decision made against the
    previous proposal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    project_id: UUID
    objective: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    proposed_engineering_scope: str = Field(min_length=1)
    known_uncertainties: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    # Synthesis facts the research evidence was judged not to support.
    unverified_claims: list[str] = Field(default_factory=list)
    evidence: list[ArtifactRef] = Field(default_factory=list)
    # Engineering bindings. V0 resolves these before approval; phase 5 uses them.
    repository: str | None = None
    base_commit: str | None = None
    permitted_paths: list[str] = Field(default_factory=list)
    test_command: str | None = None
    base_branch: str = "main"
    allows_pull_request: bool = False
    # Whether code execution was enabled when the proposal was made. An
    # approval of a non-executing proposal never authorizes execution later.
    executes_code: bool = False
    # A build: the finished work is committed into the office's own
    # project folder for it, rather than kept only as a patch.
    lands_in_source: bool = False
    # Which coding agent the owner approved to do it: codex or claude_code.
    coding_agent: str = ""
    # Whether it may have packages installed from npm and PyPI.
    installs_packages: bool = False
    # Work in a space's folder: the result lands as this office/
    # branch in the owner's repository; its default branch is never written.
    lands_on_branch: str = ""

    @property
    def base_branch_name(self) -> str:
        return self.base_branch or "main"

    def digest(self) -> str:
        """Content digest of the whole proposal.

        Computed from a canonical serialization so an equivalent proposal
        always digests identically and any change is detectable.
        """
        dumped = self.model_dump(mode="json")
        # Left out at their defaults, so a proposal approved before they
        # existed digests exactly as it did then.
        for key, default in (
            ("lands_in_source", False),
            ("coding_agent", ""),
            ("installs_packages", False),
            ("lands_on_branch", ""),
        ):
            if dumped.get(key) == default:
                dumped.pop(key, None)
        canonical = json.dumps(dumped, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class DecisionBrief(BaseModel):
    """What the human reads before deciding (SPEC section 19)."""

    model_config = ConfigDict(extra="forbid")

    objective: str
    recommendation: str
    research_summary: list[str] = Field(default_factory=list)
    critical_review: list[str] = Field(default_factory=list)
    known_uncertainties: list[str] = Field(default_factory=list)
    proposed_engineering_scope: str
    permitted_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)
    expected_output: str = ""
    executes_code: bool = False
    repository: str | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    test_command: str | None = None
    publishes: bool = False
    # A build: the owner presses Start building, and which agent builds.
    builds: bool = False
    coding_agent: str = ""
    scope_digest: str


def risk_reason(scope: ApprovalScope) -> str | None:
    """Why approving this would do more than record a decision, or None if it would not.

    Read from the bound proposal alone, never from a model: what it may run,
    write, install or publish is all in the scope.
    """
    if scope.lands_in_source or scope.lands_on_branch:
        return "writes code into a folder or repository"
    if scope.executes_code and scope.repository:
        return "runs code"
    if scope.allows_pull_request:
        return "may publish a branch"
    if scope.installs_packages:
        return "installs packages"
    return None


def low_risk(scope: ApprovalScope) -> bool:
    """Whether approving only records the decision: nothing runs, is written,
    installed or sent. Only such a proposal may be approved from chat."""
    return risk_reason(scope) is None


def approval_decision(scope: ApprovalScope, comment: str | None) -> dict[str, Any]:
    """The durable decision an explicit approval records."""
    return {
        "title": scope.objective[:255],
        "decision": scope.recommendation,
        "reason": comment or "Approved by the owner.",
        "confidence": "medium",
    }


def describe_consequence(scope: ApprovalScope) -> str:
    """State what approving will actually do, from the bound scope alone."""
    if scope.lands_in_source:
        agent = label_of(scope.coding_agent)
        if not scope.executes_code:
            return (
                "Code execution is switched off for this office, so nothing would be built. "
                "Approving records the decision only."
            )
        packages = (
            " If it needs packages, the office installs them from npm or PyPI in a separate "
            "installer, without running their install scripts."
            if scope.installs_packages
            else " It may not install packages."
        )
        if scope.lands_on_branch:
            # A space's folder (6d): the owner's own repository.
            commit = (scope.base_commit or "unresolved")[:12]
            return (
                f"{agent} writes it in the office's own copy of {scope.repository}, from "
                f"{scope.base_branch_name}@{commit}, in a sandbox with no network."
                + packages
                + f" If its checks pass, the result is saved in your repository as the branch "
                f"{scope.lands_on_branch}. Your {scope.base_branch_name} branch and your files "
                "are not changed; merging it is yours to do, separately."
            )
        return (
            f"{agent} writes it in the office's own project folder for this build, in a "
            "sandbox with no network." + packages + " The office then runs its checks with "
            "no network. If they pass, the files are kept and you can open, download and "
            "preview them. Nothing is published or deployed."
        )
    if not scope.repository:
        return (
            "No engineering target is configured. Approving records the decision; "
            "no code is executed."
        )
    if not scope.executes_code:
        return (
            "Code execution is disabled. Approving records the decision; "
            "no code is executed and nothing is published."
        )
    commit = (scope.base_commit or "unresolved")[:12]
    paths = ", ".join(scope.permitted_paths) or "no paths (nothing may change)"
    tests = f"then runs `{scope.test_command}`" if scope.test_command else "with no test command"
    publication = (
        "If the tests pass, it pushes a branch and opens a merge request."
        if scope.allows_pull_request
        else "No branch is published."
    )
    return (
        f"Approving runs the {label_of(scope.coding_agent)} engineering runtime against "
        f"{scope.repository} "
        f"at {scope.base_branch_name}@{commit}, limited to {paths}, {tests}. {publication}"
    )


def build_scope(
    run_id: UUID,
    project_id: UUID,
    objective: str,
    synthesis: dict[str, Any],
    evidence: list[ArtifactRef],
) -> ApprovalScope:
    return ApprovalScope(
        run_id=run_id,
        project_id=project_id,
        objective=objective,
        recommendation=str(synthesis["recommendation"]),
        proposed_engineering_scope=str(synthesis["proposed_engineering_scope"]),
        known_uncertainties=[str(item) for item in synthesis.get("unknowns", [])],
        acceptance_criteria=[
            str(item) for item in synthesis.get("engineering_acceptance_criteria", [])
        ],
        unverified_claims=[str(item) for item in synthesis.get("unverified_claims", [])],
        evidence=evidence,
    )


def build_brief(
    scope: ApprovalScope,
    research: dict[str, Any] | None,
    critique: dict[str, Any] | None,
) -> DecisionBrief:
    return DecisionBrief(
        objective=scope.objective,
        recommendation=scope.recommendation,
        research_summary=[str(item) for item in (research or {}).get("key_findings", [])],
        critical_review=[str(item) for item in (critique or {}).get("critical_issues", [])],
        known_uncertainties=scope.known_uncertainties,
        proposed_engineering_scope=scope.proposed_engineering_scope,
        permitted_paths=scope.permitted_paths,
        acceptance_criteria=scope.acceptance_criteria,
        unverified_claims=scope.unverified_claims,
        expected_output=describe_consequence(scope),
        executes_code=scope.executes_code and bool(scope.repository),
        repository=scope.repository,
        base_branch=scope.base_branch_name if scope.repository else None,
        base_commit=scope.base_commit,
        test_command=scope.test_command,
        publishes=scope.allows_pull_request and scope.executes_code,
        builds=scope.lands_in_source,
        coding_agent=scope.coding_agent,
        scope_digest=scope.digest(),
    )
