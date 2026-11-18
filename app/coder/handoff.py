"""An approved coding task, carried out by the coder on the owner's Mac (Stage 36b).

The office no longer runs a coding agent itself. It queues one job for the
approval, waits while the helper works, passes on the owner's Stop and
comments, and when the job ends checks the result on its own side:

1. The work's starting point: for a build, a clone of the office's own build
   folder at the approved commit; for the owner's folder, the files the helper
   sent from the base of the branch.
2. The branch's files at its end, as the helper sent them (`git archive`, so
   only what is committed on the branch), laid over that starting point.
3. The diff the office works out itself, the approved paths, the test command
   in the isolated runner, the patch kept. The CLI's account of its work is not
   evidence; this is.

A build that passes lands in the office's build folder, as before, so its
versions, preview and downloads are unchanged. Other work stays on its
`office/` branch in the owner's folder, on the Mac, for the owner to merge.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.approvals import ApprovalScope
from app.coder import jobs
from app.coder.observer import BuildObserver
from app.db.models import CoderJob
from app.policy import require
from app.runtimes.base import ExecutionError, ExecutionLimits, run_bounded
from app.runtimes.engineer import (
    BUILD_RULES,
    NO_PACKAGES,
    WORK_RULES,
    EngineeringResult,
    ResultKeeper,
    TestExecutor,
    engineering_task,
    keep_out_installed,
    render_instructions,
)
from app.runtimes.installer import wanted
from app.runtimes.steering import CONTINUE, NOTE, NOW, PAUSE
from app.runtimes.workspace import (
    ScopeViolation,
    Workspace,
    assert_within_scope,
    create_workspace,
    discard,
)
from app.schemas.engineering import EngineeringReport, EngineeringStatus

POLL_SECONDS = 2.0
# Not taken by the helper in a day: the task ends, nothing was done.
QUEUED_FOR = timedelta(hours=24)
IDENTITY = ["-c", "user.name=Quarterdeck", "-c", "user.email=office@localhost"]
KEY = re.compile(r"[^a-z0-9-]+")
STOPPED = "runtime:stopped_by_owner"
NO_PAUSE = (
    "Pause is not available while your Mac's coder works: comment, or Stop it. "
    "You can also carry it on yourself with the command shown when it ends."
)


def failed(error: str, summary: str = "") -> dict[str, Any]:
    return EngineeringReport(
        status=EngineeringStatus.FAILED, errors=[error], summary=summary[:2000]
    ).model_dump()


# A build that may use packages, written on the Mac and checked by the office.
MAC_PACKAGES = """- If it needs packages, list them in package.json (npm) or requirements.txt
  (pip, ready-made wheels only, pinned). You may install them here to try it,
  but never commit node_modules/ or py_packages/: before it checks, the office
  installs exactly what the lists say (npm without install scripts; pip into
  py_packages/), with no network for the build itself. check.sh must work with
  them installed that way: for Python, put py_packages on PYTHONPATH. Prefer
  few, well-known packages; plain files when they will do."""


def brief_of(scope: ApprovalScope) -> str:
    """What the CLI is told: the approved task, and the rules for where it works."""
    told = render_instructions(engineering_task(scope))
    if scope.lands_on_branch:
        return told + WORK_RULES.format(check=scope.test_command or "the project's checks")
    if not scope.lands_in_source:
        return told
    return told + BUILD_RULES.format(
        packages=MAC_PACKAGES if scope.installs_packages else NO_PACKAGES
    )


def is_build(scope: ApprovalScope) -> bool:
    return scope.lands_in_source and not scope.lands_on_branch


def target_of(scope: ApprovalScope) -> tuple[str, str, str]:
    """The folder under ~/dev, the branch, and a build's office folder to seed from."""
    if scope.lands_on_branch:
        return str(scope.repository or ""), scope.lands_on_branch, ""
    if is_build(scope):
        key = KEY.sub("-", Path(scope.repository or "").name.lower()).strip("-") or "build"
        branch = f"{jobs.BRANCH_PREFIX}{key[:40]}-{str(scope.run_id)[:8]}"
        return f"office-builds/{key}", branch, str(scope.repository)
    raise jobs.JobRefused("coder_needs_a_folder")


def _extract(archive: Path, into: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(into, filter="data")


async def _git(args: list[str], where: Path) -> str:
    done = await run_bounded(["git", *args], where, ExecutionLimits(timeout_seconds=120))
    if not done.succeeded:
        raise ExecutionError(f"git_{args[0]}_failed", done.stderr[:200])
    return done.stdout.strip()


async def workspace_from(archive: Path, root: Path, attempt: UUID) -> Workspace:
    """A fresh repository holding the files of the branch's start."""
    folder = (root / str(attempt)).resolve()
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    await asyncio.to_thread(_extract, archive, folder)
    await _git(["init", "-q", "-b", "main"], folder)
    await _git(["add", "-A"], folder)
    await _git([*IDENTITY, "commit", "-q", "--allow-empty", "-m", "Start"], folder)
    head = await _git(["rev-parse", "HEAD"], folder)
    return Workspace(root=root, repository=folder, base_commit=head, branch="main")


def _lay_over(repository: Path, archive: Path) -> None:
    """The branch's end, exactly: what it removed goes too."""
    for entry in repository.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    _extract(archive, repository)


@dataclass
class CoderHandoff:
    """The engineering executor: the coder on the Mac does the work."""

    session_factory: sessionmaker[Session]
    engineering_root: Path
    test_executor: TestExecutor
    test_timeout_seconds: float = 900.0
    observer: BuildObserver | None = None
    invocations: int = 0
    poll_seconds: float = POLL_SECONDS
    # Installs a build's listed packages before its checks, when its approval allows.
    installer: Any = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    async def execute(self, scope: ApprovalScope, approved: bool) -> dict[str, Any]:
        require("modify_code", approved=approved)
        if scope.allows_pull_request:
            require("create_pull_request", approved=approved)
        if not scope.repository:
            return failed("no_repository_configured")
        # A proposal approved as one that runs no code is not extended later.
        if not scope.executes_code:
            return failed("approval_does_not_cover_execution")
        self.invocations += 1
        try:
            job_id = self._queue(scope)
        except jobs.JobRefused as refused:
            return failed(f"coder:{refused.code}")
        return await self._carry_out(scope, job_id)

    async def reattach(self, scope: ApprovalScope) -> dict[str, Any] | None:
        """After a restart: the job already handed out for this approval, waited
        on again rather than repeated. None when there is none."""
        with self.session_factory() as session:
            job = jobs.job_for(session, scope.run_id, scope.digest())
            job_id = job.id if job is not None else None
        if job_id is None:
            return None
        return await self._carry_out(scope, job_id)

    async def send_back(self, scope: ApprovalScope, notes: str) -> dict[str, Any]:
        """The owner's Send back: the same CLI session, on the same worktree and
        branch, told what to change; then checked again as the first round was."""
        require("modify_code", approved=True)
        with self.session_factory() as session, session.begin():
            earlier = jobs.job_for(session, scope.run_id, scope.digest())
            if earlier is None or earlier.state not in jobs.CLOSED:
                raise jobs.JobRefused("nothing_to_send_back")
            job = jobs.queue_job(
                session,
                run_id=scope.run_id,
                folder=earlier.folder,
                branch=earlier.branch,
                cli=earlier.cli,
                brief=SENT_BACK.format(notes=notes.strip()),
                base_branch=earlier.base_branch,
                base_commit=earlier.base_commit,
                scope_digest=earlier.scope_digest,
                seed_from=earlier.seed_from,
            )
            job.session = earlier.session
            job_id, label = job.id, jobs.CLIS[job.cli]
        self._say({"kind": "phase", "text": f"Sent back to {label} on your Mac, with your notes."})
        return await self._carry_out(scope, job_id)

    def _queue(self, scope: ApprovalScope) -> UUID:
        folder, branch, seed = target_of(scope)
        with self.session_factory() as session, session.begin():
            job = jobs.queue_job(
                session,
                run_id=scope.run_id,
                folder=folder,
                branch=branch,
                cli=scope.coding_agent or "claude_code",
                brief=brief_of(scope),
                base_branch=scope.base_branch_name,
                base_commit=scope.base_commit,
                scope_digest=scope.digest(),
                seed_from=seed,
            )
            connected = jobs.connected(session, self.clock())
            label = jobs.CLIS[job.cli]
            job_id = job.id
        self._say({"kind": "phase", "text": f"Handed to {label} on your Mac, on {branch}."})
        if not connected:
            self._say({"kind": "phase", "text": AWAY, "ok": False})
        return job_id

    async def _carry_out(self, scope: ApprovalScope, job_id: UUID) -> dict[str, Any]:
        view = await self._wait(job_id)
        outcome = dict(view.get("outcome") or {})
        message = str(outcome.get("message") or "")
        state = view["state"]
        if state == "cancelled":
            return failed(STOPPED, message)
        if state == "unknown":
            return failed("coder:unknown", message)
        if state != "done":
            return failed(f"coder:{outcome.get('error') or 'cli_failed'}", message)
        result = await self._checked(scope, job_id, view)
        report = result.report().model_dump()
        if outcome.get("take_over"):
            report["summary"] = (
                f"{report.get('summary') or ''}\n\nCarry it on yourself: {outcome['take_over']}"
            ).strip()[:2000]
        return report

    async def _wait(self, job_id: UUID) -> dict[str, Any]:
        """Until the job ends, passing on the owner's Stop and comments."""
        while True:
            taken: list[str] = []
            with self.session_factory() as session, session.begin():
                job = session.get(CoderJob, job_id)
                if job is None:
                    return {"state": "unknown", "outcome": {}}
                if job.state in (*jobs.CLOSED, "unknown"):
                    return jobs.job_view(job)
                if job.state == "queued" and self.clock() - job.created_at > QUEUED_FOR:
                    jobs.ask_stop(session, job_id, self.clock())
                    view = jobs.job_view(job)
                    view["outcome"] = {"error": "coder_not_started"}
                    view["state"] = "failed"
                    return view
                if self.observer is not None:
                    if self.observer.stop():
                        jobs.ask_stop(session, job_id, self.clock())
                    taken = self._pass_on(session, job_id)
            if taken and self.observer is not None:
                self.observer.take(taken)
            await asyncio.sleep(self.poll_seconds)

    def _pass_on(self, session: Session, job_id: UUID) -> list[str]:
        """The owner's comments to the job; each taken once."""
        assert self.observer is not None  # noqa: S101 - checked by the caller
        taken: list[str] = []
        for note in self.observer.notes():
            kind, text = note.get("kind"), str(note.get("text") or "")
            if kind in (NOTE, NOW) and text.strip():
                try:
                    jobs.add_note(session, job_id, text, now=kind == NOW)
                except jobs.JobRefused:
                    continue
            elif kind in (PAUSE, CONTINUE):
                self._say({"kind": "phase", "text": NO_PAUSE, "ok": False})
            taken.append(str(note["id"]))
        return taken

    async def _checked(
        self, scope: ApprovalScope, job_id: UUID, view: dict[str, Any]
    ) -> EngineeringResult:
        """The office's own look at the branch: diff, paths, checks, patch."""
        attempt = uuid4()
        outcome = dict(view.get("outcome") or {})
        files = jobs.files_of(self.engineering_root, job_id)
        head = files / "head.tar"
        if not head.is_file():
            return EngineeringResult(attempt, False, "coder_files_missing", branch=view["branch"])
        build = is_build(scope)
        try:
            if build:
                workspace = await create_workspace(
                    Path(str(scope.repository)), self.engineering_root, attempt, scope.base_commit
                )
            elif (files / "base.tar").is_file():
                workspace = await workspace_from(files / "base.tar", self.engineering_root, attempt)
            else:
                return EngineeringResult(
                    attempt, False, "coder_files_missing", branch=view["branch"]
                )
        except ExecutionError as error:
            return EngineeringResult(attempt, False, error.code, branch=view["branch"])
        keeper = ResultKeeper(self.engineering_root)
        try:
            await asyncio.to_thread(_lay_over, workspace.repository, head)
            if build:
                keep_out_installed(workspace.repository)
                stopped = await self._packages(scope, workspace)
                if stopped is not None:
                    return EngineeringResult(attempt, False, stopped, branch=view["branch"])
            said = SimpleNamespace(final_message=str(outcome.get("message") or ""))
            result = await keeper.what_changed(scope, workspace, said, attempt)
            result.branch = view["branch"]
            if result.reason != "tests_not_run":
                return result
            if not scope.test_command:
                result.reason = "no_test_command"
                return result
            if not await self._tests(scope, workspace, result):
                return result
            await keeper.keep_patch(result, workspace)
            result.completed, result.reason = True, "completed"
            if build:
                await keeper.land(result, workspace, Path(str(scope.repository)), scope)
                self._say(
                    {"kind": "phase", "text": "Saved in the office's project folder.", "ok": True}
                    if result.landed
                    else {"kind": "phase", "text": "Built, but it could not be saved.", "ok": False}
                )
            else:
                result.landed, result.commit = True, str(outcome.get("head") or "") or None
                self._say(
                    {
                        "kind": "phase",
                        "text": f"On your branch {view['branch']} in {view['folder']}, "
                        "for you to merge.",
                        "ok": True,
                    }
                )
            return result
        except (OSError, tarfile.TarError, ExecutionError):
            return EngineeringResult(
                attempt, False, "coder_files_unreadable", branch=view["branch"]
            )
        finally:
            discard(workspace)

    async def _packages(self, scope: ApprovalScope, workspace: Workspace) -> str | None:
        """What the build lists, installed by the office's own installer (network,
        no secrets, no install scripts). Why it stops, or None to go on."""
        kinds = wanted(workspace.repository)
        if not kinds:
            return None
        if not scope.installs_packages or self.installer is None:
            self._say(
                {
                    "kind": "phase",
                    "text": "It listed packages, but this build may not install them.",
                    "ok": False,
                }
            )
            return None  # Its checks decide whether it works without them.
        for kind in kinds:
            self._say(
                {"kind": "phase", "text": f"Installing its {kind} packages, without their scripts."}
            )
            try:
                installed = await self.installer.install(kind, workspace.repository)
            except ExecutionError as error:
                self._say(
                    {
                        "kind": "check",
                        "text": f"The installer was not reachable ({error.code}).",
                        "ok": False,
                    }
                )
                return f"install:{error.code}"
            tail = (installed.stderr or installed.stdout).strip().splitlines()[-1:] or [""]
            self._say(
                {
                    "kind": "check",
                    "text": ("Installed" if installed.succeeded else "Install failed")
                    + (f": {tail[0][:200]}" if tail[0] else "."),
                    "ok": installed.succeeded,
                }
            )
            if not installed.succeeded:
                return "install_failed"
        return None

    async def _tests(
        self, scope: ApprovalScope, workspace: Workspace, result: EngineeringResult
    ) -> bool:
        changed = result.changed_paths
        self._say(
            {
                "kind": "phase",
                "text": f"{len(changed)} file{'' if len(changed) == 1 else 's'} changed. "
                f"The office is checking it: {scope.test_command}",
            }
        )
        try:
            tests = await self.test_executor.run(
                shlex.split(scope.test_command or ""),
                workspace.repository,
                ExecutionLimits(timeout_seconds=self.test_timeout_seconds),
            )
        except ExecutionError as error:
            result.reason = f"tests:{error.code}"
            self._say({"kind": "check", "text": "The check could not run.", "ok": False})
            return False
        result.test_output = (tests.stdout + tests.stderr)[-20_000:]
        result.test_exit_code = tests.exit_code
        last = (
            result.test_output.strip().splitlines()[-1][:200] if result.test_output.strip() else ""
        )
        self._say(
            {
                "kind": "check",
                "text": ("Checks passed" if tests.succeeded else "Checks failed")
                + (f": {last}" if last else "."),
                "ok": tests.succeeded,
            }
        )
        if not tests.succeeded:
            result.reason = "tests_failed"
            return False
        # The test command is repository code and may change files itself.
        try:
            result.changed_paths = await assert_within_scope(workspace, scope.permitted_paths)
        except ScopeViolation as violation:
            result.changed_paths = violation.paths
            result.reason = "out_of_scope_after_tests"
            return False
        return True

    def _say(self, step: dict[str, Any]) -> None:
        if self.observer is not None:
            self.observer.step(step)


SENT_BACK = """The owner reviewed what you did and sends it back:

{notes}

Change what they ask, on the files you left here, and finish as before."""

AWAY = (
    "Quarterdeck Coder is not running on your Mac, so this waits for it. Start it "
    "from the office's folder: .venv/bin/python -m scripts.mac_coder start"
)
