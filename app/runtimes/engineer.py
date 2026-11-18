"""What the office does with coding the coder on the owner's Mac has done
(Stage 36): the record of an attempt, its diff within the approved paths, the
patch it keeps, and landing a build in its project folder. The rules a coding
CLI is given come from here too.

The office runs no coding agent itself any more; `app/coder/handoff.py` hands
the work to the Mac and uses these to check and keep what comes back.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from app.approvals import ApprovalScope
from app.runtimes.base import ExecutionError, ExecutionLimits, ExecutionResult, run_bounded
from app.runtimes.installer import INSTALLED
from app.runtimes.workspace import (
    ScopeViolation,
    Workspace,
    assert_within_scope,
    diff,
    write_patch,
)
from app.schemas.engineering import (
    EngineeringReport,
    EngineeringStatus,
    EngineeringTask,
    TestReport,
    TestStatus,
)
from app.workspace_git import BranchRefused, land_on_branch

# Where whole patches are kept, beside the attempt clones and outlasting them.
PATCHES = "patches"
# How much of a patch the report itself carries; the file holds all of it.
PREVIEW_CHARS = 200_000


class TestExecutor(Protocol):
    """Runs a repository-controlled command. See app/runtimes/isolated.py."""

    async def run(
        self, command: list[str], cwd: Path, limits: ExecutionLimits | None = None
    ) -> ExecutionResult: ...


class LocalExecutor:
    """In-process execution, for development and tests only.

    Production uses the isolated runner, because a repository's tests must not
    run beside application credentials.
    """

    async def run(
        self, command: list[str], cwd: Path, limits: ExecutionLimits | None = None
    ) -> ExecutionResult:
        return await run_bounded(command, cwd, limits)


@dataclass
class EngineeringResult:
    """Internal attempt record. `report` is the SPEC section 20 result."""

    attempt_id: UUID
    completed: bool
    reason: str
    changed_paths: list[str] = field(default_factory=list)
    diff: str = ""
    test_output: str = ""
    test_exit_code: int | None = None
    runtime_message: str = ""
    branch: str = ""
    pull_request_url: str | None = None
    publication_error: str | None = None
    diff_truncated: bool = False
    patch_path: str | None = None
    patch_sha256: str | None = None
    # Committed into the build's project folder, as this version.
    landed: bool = False
    commit: str | None = None

    def report(self) -> EngineeringReport:
        """Assemble the structured result from evidence, not from claims."""
        if self.test_exit_code is None:
            tests = TestReport(status=TestStatus.NOT_RUN, details=self.reason)
        elif self.test_exit_code == 0:
            tests = TestReport(status=TestStatus.PASSED, details=self.test_output[-2000:])
        else:
            tests = TestReport(status=TestStatus.FAILED, details=self.test_output[-2000:])
        errors = [] if self.completed else [self.reason]
        if self.publication_error:
            errors = [*errors, self.publication_error]
        return EngineeringReport(
            status=EngineeringStatus.COMPLETED if self.completed else EngineeringStatus.FAILED,
            branch=self.branch or None,
            changed_files=list(self.changed_paths),
            tests=tests,
            pull_request_url=self.pull_request_url,
            summary=self.runtime_message[:2000],
            errors=errors,
            diff=self.diff,
            diff_truncated=self.diff_truncated,
            patch_path=self.patch_path,
            patch_sha256=self.patch_sha256,
            landed=self.landed,
            commit=self.commit,
        )


class ResultKeeper:
    """Checks and keeps a finished attempt: its diff, its patch, its landing."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    @staticmethod
    async def what_changed(
        scope: ApprovalScope, workspace: Workspace, outcome: Any, attempt_id: UUID
    ) -> EngineeringResult:
        """The files written, inside the approved paths, and their diff; a result
        with "tests_not_run" to go on with, or one that ends the attempt."""
        try:
            changed = await assert_within_scope(workspace, scope.permitted_paths)
        except ScopeViolation as violation:
            return EngineeringResult(
                attempt_id,
                False,
                "out_of_scope",
                changed_paths=violation.paths,
                runtime_message=outcome.final_message,
                branch=workspace.branch,
            )
        if not changed:
            return EngineeringResult(
                attempt_id,
                False,
                "no_changes",
                runtime_message=outcome.final_message,
                branch=workspace.branch,
            )
        body = await diff(workspace)
        return EngineeringResult(
            attempt_id,
            False,
            "tests_not_run",
            changed_paths=changed,
            diff=body[:PREVIEW_CHARS],
            runtime_message=outcome.final_message,
            branch=workspace.branch,
        )

    async def keep_patch(self, result: EngineeringResult, workspace: Workspace) -> None:
        patch = self.workspace_root / PATCHES / f"{result.attempt_id}.patch"
        await write_patch(workspace, patch)
        data = patch.read_bytes()
        text = data.decode("utf-8", errors="replace")
        result.diff = text[:PREVIEW_CHARS]
        result.diff_truncated = len(text) > PREVIEW_CHARS
        result.patch_path = str(patch)
        result.patch_sha256 = hashlib.sha256(data).hexdigest()

    async def land(
        self, result: EngineeringResult, workspace: Workspace, source: Path, scope: ApprovalScope
    ) -> None:
        """Commit the finished build into its project folder.

        Only after the checks passed and the patch was kept. The folder moves
        forward only by fast-forward: if it changed meanwhile, nothing is
        overwritten and the patch is still there.
        """
        identity = ["-c", "user.name=Quarterdeck", "-c", "user.email=office@localhost"]
        steps = [
            (["git", "add", "-A"], workspace.repository),
            (
                [
                    "git",
                    *identity,
                    "commit",
                    "-q",
                    "-m",
                    f"Build: {scope.objective[:72]}",
                    # Which task made this version, read back by the version list.
                    "-m",
                    f"Office-Run: {scope.run_id}",
                ],
                workspace.repository,
            ),
        ]
        if not scope.lands_on_branch:
            # The office's own build folder moves forward by fast-forward.
            steps += [
                (["git", "fetch", "-q", str(workspace.repository), workspace.branch], source),
                (["git", "merge", "--ff-only", "-q", "FETCH_HEAD"], source),
            ]
        for command, where in steps:
            try:
                done = await run_bounded(command, where, ExecutionLimits(timeout_seconds=120))
            except ExecutionError:
                result.publication_error = "landing_failed"
                return
            if not done.succeeded:
                result.publication_error = "landing_failed"
                return
        if scope.lands_on_branch:
            # A space's folder (6d): only an office/ branch is written there,
            # never the owner's default branch or checkout.
            try:
                result.commit = await land_on_branch(
                    workspace.repository, workspace.branch, source, scope.lands_on_branch
                )
            except BranchRefused:
                result.publication_error = "landing_failed"
                return
            result.landed = True
            return
        result.landed = True
        head = await run_bounded(["git", "rev-parse", "HEAD"], source)
        if head.succeeded:
            result.commit = head.stdout.strip()


BUILD_RULES = """

This build lives in its own folder. Make it complete and working:
- If the folder already holds an earlier version, build on it: keep what still
  fits the spec. Update check.sh first, so that it checks THIS version: an old
  check.sh checks the old version and will fail the new one.
- Write every file it needs, with real content, no placeholders.
- Write README.md: what it is, how to open or run it, and how to change it.
- Write check.sh, a POSIX sh script that checks the build offline and exits
  non-zero if anything is wrong: the files it needs exist, and the code is
  sound (for example `node --check` on each JavaScript file, or a short
  python3 script that parses the HTML and finds the parts the spec names). It
  runs without network, with sh, python3 and node available.
- Keep only the build's own files here: no planning or state folders of your
  own tools (such as .omx/ or .codex/).
{packages}"""

NO_PACKAGES = "- Keep it to plain files that work as they are: no package installs."

# What work in the owner's own project is told (6d): theirs, not a build.
WORK_RULES = """

This is the owner's own project, in a copy of it on a branch of its own.
- Change only what the task needs; keep the project's own structure, style and
  conventions, and leave unrelated files as they are.
- Do not add files of your own tools (such as .omx/ or .codex/) or build
  scripts the project does not use.
- The office checks the result with: {check}. Make it pass.
- The result is offered to the owner as a branch; they decide whether to merge."""


# The agent's own account of its sandbox failing under it (seen 9 Oct, twice).
SANDBOX_FAULT = re.compile(
    r"ENOENT|基础设施|infrastructure (?:failure|fault|error)|filesystem (?:became |was |is )?"
    r"(?:unavailable|inaccessible)|sandbox (?:failure|fault|error)",
    re.IGNORECASE,
)


def keep_out_installed(folder: Path) -> None:
    """Installed packages never become part of the build's saved files."""

    ignore = folder / ".gitignore"
    have = ignore.read_text().splitlines() if ignore.is_file() else []
    missing = [entry for entry in INSTALLED if entry not in have]
    if missing:
        ignore.write_text("\n".join([*have, *missing]).strip() + "\n")


# Fixed by policy, not proposed by a model and not changeable per task.
POLICY_CONSTRAINTS = [
    "Change only the permitted paths.",
    "Add no dependencies the rules below do not allow.",
    "Do not push, merge, deploy or publish anything; the office and the owner do that.",
]


def engineering_task(scope: ApprovalScope) -> EngineeringTask:
    """The SPEC section 20 contract, built only from an approved scope.

    Callers reach this after the approval check, so `approved` reflects the
    recorded decision, never a submitted flag.
    """
    return EngineeringTask(
        task_id=str(scope.run_id),
        project_id=str(scope.project_id),
        repository=scope.repository or "unconfigured",
        base_branch=scope.base_branch_name,
        objective=scope.objective,
        requirements_artifact=scope.proposed_engineering_scope,
        constraints=list(POLICY_CONSTRAINTS),
        acceptance_criteria=list(scope.acceptance_criteria),
        permitted_paths=list(scope.permitted_paths),
        test_command=scope.test_command,
        approved=True,
    )


def render_instructions(task: EngineeringTask) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- (none stated)"

    tests = task.test_command or "(no test command configured)"
    return (
        f"Objective:\n{task.objective}\n\n"
        f"Approved engineering scope:\n{task.requirements_artifact}\n\n"
        f"Acceptance criteria:\n{bullets(task.acceptance_criteria)}\n\n"
        f"Permitted paths:\n{bullets(task.permitted_paths)}\n\n"
        f"Constraints:\n{bullets(task.constraints)}\n\n"
        f"The application will verify the result by running: {tests}"
    )
