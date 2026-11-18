"""An approved coding task carried out by the coder on the Mac (Stage 36b): the
office queues the job, passes on Stop and comments, and checks the result itself."""

import asyncio
import io
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app import builds
from app.approvals import ApprovalScope
from app.coder import jobs
from app.coder.handoff import STOPPED, CoderHandoff, brief_of, target_of
from app.coder.observer import BuildObserver
from app.db.models import CoderJob
from app.runtimes.engineer import LocalExecutor
from app.schemas.engineering import EngineeringStatus

pytestmark = pytest.mark.integration


def factory(client: httpx.AsyncClient) -> Any:
    return client.app_state.session_factory  # type: ignore[attr-defined]


async def a_run(client: httpx.AsyncClient) -> tuple[str, str]:
    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()
    return str(run["id"]), str(project)


def a_tar(files: dict[str, str]) -> bytes:
    held = io.BytesIO()
    with tarfile.open(fileobj=held, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return held.getvalue()


def space_scope(run_id: str, project_id: str, **given: Any) -> ApprovalScope:
    fields: dict[str, Any] = {
        "objective": "Add a page",
        "recommendation": "Do it",
        "proposed_engineering_scope": "Add page.txt",
        "repository": "clients/site",
        "base_branch": "main",
        "permitted_paths": ["*"],
        "test_command": "sh check.sh",
        "executes_code": True,
        "lands_in_source": True,
        "lands_on_branch": "office/add-a-page-abc123",
        "coding_agent": "claude_code",
    }
    return ApprovalScope(run_id=UUID(run_id), project_id=UUID(project_id), **(fields | given))


class Steps:
    """The task's live view, and the owner's Stop and comments."""

    def __init__(self) -> None:
        self.said: list[dict[str, Any]] = []
        self.stopping = False
        self.waiting: list[dict[str, Any]] = []
        self.taken: list[str] = []

    def observer(self) -> BuildObserver:
        return BuildObserver(
            step=self.said.append,
            stop=lambda: self.stopping,
            notes=lambda: [note for note in self.waiting if note["id"] not in self.taken],
            take=self.taken.extend,
        )

    def texts(self) -> list[str]:
        return [str(step.get("text")) for step in self.said]


def executor(client: httpx.AsyncClient, root: Path, steps: Steps) -> CoderHandoff:
    return CoderHandoff(
        session_factory=factory(client),
        engineering_root=root,
        test_executor=LocalExecutor(),
        observer=steps.observer(),
        poll_seconds=0.02,
    )


class Helper:
    """The helper on the Mac, played in-process against the same database."""

    def __init__(self, client: httpx.AsyncClient, root: Path) -> None:
        self.client, self.root = client, root
        self.notes: list[str] = []
        self.stop_seen = False

    async def claim(self) -> dict[str, Any]:
        for _ in range(500):
            with factory(self.client)() as session, session.begin():
                job = jobs.claim_next(session, datetime.now(UTC))
            if job is not None:
                return job
            await asyncio.sleep(0.01)
        raise AssertionError("no job was queued")

    def report(self, job_id: str) -> dict[str, Any]:
        with factory(self.client)() as session, session.begin():
            answer = jobs.heard(session, UUID(job_id), [], datetime.now(UTC))
        self.notes += answer["notes"]
        self.stop_seen = self.stop_seen or answer["stop"]
        return answer

    def finish(self, job_id: str, files: dict[str, bytes], **outcome: Any) -> None:
        folder = jobs.files_of(self.root, UUID(job_id))
        folder.mkdir(parents=True, exist_ok=True)
        for which, data in files.items():
            (folder / f"{which}.tar").write_bytes(data)
        with factory(self.client)() as session, session.begin():
            jobs.finish(session, UUID(job_id), {"exit_code": 0} | outcome, datetime.now(UTC))


BASE = a_tar({"README.md": "site\n", "check.sh": "test -f page.txt\n"})
HEAD = a_tar({"README.md": "site\n", "check.sh": "test -f page.txt\n", "page.txt": "hi\n"})


async def test_the_owners_folder_work_is_checked_by_the_office_and_stays_on_its_branch(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    scope = space_scope(run_id, project_id)
    steps, helper = Steps(), Helper(client, tmp_path)

    async def on_the_mac() -> None:
        job = await helper.claim()
        assert job["folder"] == "clients/site" and job["branch"] == "office/add-a-page-abc123"
        assert job["seed"] is False and "Add page.txt" in job["brief"]
        helper.finish(
            job["id"],
            {"base": BASE, "head": HEAD},
            branch=job["branch"],
            head="f00d",
            message="Added page.txt",
            take_over="cd x && claude --resume s",
        )

    report, _ = await asyncio.gather(
        executor(client, tmp_path, steps).execute(scope, approved=True), on_the_mac()
    )
    assert report["status"] == EngineeringStatus.COMPLETED
    # The office worked out what changed, from the files themselves.
    assert report["changed_files"] == ["page.txt"]
    assert report["tests"]["status"] == "passed"
    assert report["branch"] == "office/add-a-page-abc123" and report["commit"] == "f00d"
    assert "Carry it on yourself: cd x && claude --resume s" in report["summary"]
    assert any("for you to merge" in text for text in steps.texts())


async def test_a_change_outside_the_approved_paths_is_refused(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    scope = space_scope(run_id, project_id, permitted_paths=["docs/*"])
    helper = Helper(client, tmp_path)

    async def on_the_mac() -> None:
        job = await helper.claim()
        helper.finish(job["id"], {"base": BASE, "head": HEAD}, branch=job["branch"])

    report, _ = await asyncio.gather(
        executor(client, tmp_path, Steps()).execute(scope, approved=True), on_the_mac()
    )
    assert report["status"] == EngineeringStatus.FAILED
    assert report["errors"] == ["out_of_scope"]


async def test_failing_checks_fail_the_task_whatever_the_cli_said(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    helper = Helper(client, tmp_path)
    broken = a_tar({"README.md": "site\n", "check.sh": "exit 3\n", "page.txt": "hi\n"})

    async def on_the_mac() -> None:
        job = await helper.claim()
        helper.finish(job["id"], {"base": BASE, "head": broken}, message="All checks pass!")

    report, _ = await asyncio.gather(
        executor(client, tmp_path, Steps()).execute(space_scope(run_id, project_id), approved=True),
        on_the_mac(),
    )
    assert report["status"] == EngineeringStatus.FAILED
    assert report["errors"] == ["tests_failed"] and report["tests"]["status"] == "failed"


def git(*args: str, where: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed git commands
        ["git", *args],  # noqa: S607
        cwd=where,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def test_a_build_starts_from_the_offices_version_and_lands_in_its_folder(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    settings = client.settings  # type: ignore[attr-defined]
    folder = builds.folder_for(settings, run_id)
    builds.ensure_repository(folder)
    base = git("rev-parse", "HEAD", where=folder)
    scope = space_scope(
        run_id,
        project_id,
        repository=str(folder),
        base_commit=base,
        lands_on_branch="",
        permitted_paths=list(builds.PERMITTED),
        test_command=builds.CHECK_COMMAND,
    )
    key, branch, seed = target_of(scope)
    assert key == f"office-builds/{run_id}" and seed == str(folder)
    helper = Helper(client, settings.engineering_root)
    built = a_tar(
        {".gitignore": "node_modules/\npy_packages/\n", "check.sh": "true\n", "index.html": "<p>"}
    )

    async def on_the_mac() -> None:
        job = await helper.claim()
        assert job["seed"] is True and job["branch"] == branch
        seed_reply = await client.get(f"/coder/jobs/{job['id']}/seed")
        with tarfile.open(fileobj=io.BytesIO(seed_reply.content)) as tar:
            assert tar.getnames() == [".gitignore"]
        helper.finish(job["id"], {"head": built}, branch=branch)

    steps = Steps()
    report, _ = await asyncio.gather(
        CoderHandoff(
            session_factory=factory(client),
            engineering_root=settings.engineering_root,
            test_executor=LocalExecutor(),
            observer=steps.observer(),
            poll_seconds=0.02,
        ).execute(scope, approved=True),
        on_the_mac(),
    )
    assert report["status"] == EngineeringStatus.COMPLETED, report
    assert report["landed"] is True
    assert git("rev-parse", "HEAD", where=folder) == report["commit"] != base
    assert sorted(git("ls-files", where=folder).splitlines()) == [
        ".gitignore",
        "check.sh",
        "index.html",
    ]
    assert "Saved in the office's project folder." in steps.texts()


async def test_the_owners_stop_and_comments_reach_the_job(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    steps, helper = Steps(), Helper(client, tmp_path)
    steps.waiting = [
        {"id": "n1", "kind": "build_now", "text": "Use red"},
        {"id": "n2", "kind": "build_pause", "text": ""},
    ]

    async def on_the_mac() -> None:
        job = await helper.claim()
        for _ in range(200):
            answer = helper.report(job["id"])
            if answer["notes"]:
                assert answer["now"] is True
                steps.stopping = True
            if helper.stop_seen:
                break
            await asyncio.sleep(0.02)
        helper.finish(job["id"], {}, stopped=True)

    report, _ = await asyncio.gather(
        executor(client, tmp_path, steps).execute(space_scope(run_id, project_id), approved=True),
        on_the_mac(),
    )
    assert helper.notes == ["Use red"] and helper.stop_seen
    assert steps.taken == ["n1", "n2"]
    assert any("Pause is not available" in text for text in steps.texts())
    assert report["errors"] == [STOPPED]


async def test_after_a_restart_the_same_job_is_waited_on_not_handed_out_again(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    scope = space_scope(run_id, project_id)
    nothing_yet = await executor(client, tmp_path, Steps()).reattach(scope)
    assert nothing_yet is None
    first = asyncio.create_task(executor(client, tmp_path, Steps()).execute(scope, approved=True))
    helper = Helper(client, tmp_path)
    job = await helper.claim()
    first.cancel()  # The office restarts while the Mac works on.
    helper.finish(job["id"], {"base": BASE, "head": HEAD}, branch=job["branch"], head="beef")
    report = await executor(client, tmp_path, Steps()).reattach(scope)
    assert report is not None and report["status"] == EngineeringStatus.COMPLETED
    with factory(client)() as session:
        assert session.query(CoderJob).count() == 1


async def test_a_task_waits_and_says_so_while_the_helper_is_away(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    steps = Steps()
    steps.stopping = True  # Stopped while it waited: the job never starts.
    report = await executor(client, tmp_path, steps).execute(
        space_scope(run_id, project_id), approved=True
    )
    assert any("is not running on your Mac" in text for text in steps.texts())
    assert report["errors"] == [STOPPED]
    assert (await client.post("/coder/jobs/next")).json() == {"job": None}


async def test_work_access_is_given_only_while_the_helper_is_connected(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    space = (await client.post("/ventures", json={"name": f"S {uuid4().hex[:6]}"})).json()["id"]
    folder = {"name": "Site", "path": "clients/site", "access": "work"}
    refused = await client.post(f"/ventures/{space}/workspaces", json=folder)
    assert refused.status_code == 409 and refused.json()["detail"] == "work_access_off"
    await client.post("/coder/hello", json={"version": "36b", "root": "/Users/me/dev"})
    allowed = await client.post(f"/ventures/{space}/workspaces", json=folder)
    assert allowed.status_code in (200, 201), allowed.text


class Installs:
    """The office's installer: records what it was asked, and puts a package in place."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def install(self, kind: str, cwd: Path) -> Any:
        from app.runtimes.base import ExecutionResult

        self.asked.append(kind)
        (cwd / "py_packages" / "magic").mkdir(parents=True)
        (cwd / "py_packages" / "magic" / "__init__.py").write_text("OK = 1\n")
        return ExecutionResult(
            command=("pip",), exit_code=0, stdout="installed", stderr="", duration_seconds=0.1
        )


async def test_a_builds_listed_packages_are_installed_by_the_office_before_its_checks(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, project_id = await a_run(client)
    settings = client.settings  # type: ignore[attr-defined]
    folder = builds.folder_for(settings, run_id)
    builds.ensure_repository(folder)
    scope = space_scope(
        run_id,
        project_id,
        repository=str(folder),
        base_commit=git("rev-parse", "HEAD", where=folder),
        lands_on_branch="",
        permitted_paths=list(builds.PERMITTED),
        test_command=builds.CHECK_COMMAND,
        installs_packages=True,
    )
    assert "the office installs exactly what the lists say" in " ".join(brief_of(scope).split())
    helper, installer = Helper(client, settings.engineering_root), Installs()
    built = a_tar(
        {
            ".gitignore": "node_modules/\npy_packages/\n",
            "requirements.txt": "magic==1.0\n",
            "check.sh": "test -f py_packages/magic/__init__.py\n",
        }
    )

    async def on_the_mac() -> None:
        job = await helper.claim()
        helper.finish(job["id"], {"head": built}, branch=job["branch"])

    report, _ = await asyncio.gather(
        CoderHandoff(
            session_factory=factory(client),
            engineering_root=settings.engineering_root,
            test_executor=LocalExecutor(),
            observer=Steps().observer(),
            poll_seconds=0.02,
            installer=installer,
        ).execute(scope, approved=True),
        on_the_mac(),
    )
    assert installer.asked == ["pip"]
    assert report["status"] == EngineeringStatus.COMPLETED, report
    # What was installed is never part of what the build keeps.
    assert {"check.sh", "requirements.txt"} <= set(report["changed_files"])
    assert not [path for path in report["changed_files"] if path.startswith("py_packages")]


# ---- the review, Merge, Discard and Send back (36c) ----


async def approved_task(client: httpx.AsyncClient, tmp_path: Path, **given: Any) -> tuple[str, Any]:
    """A task whose coding was approved, done and checked: its report stored."""
    from app.db.models import Approval, Run

    run_id, project_id = await a_run(client)
    scope = space_scope(run_id, project_id, **given)
    helper = Helper(client, tmp_path)

    async def on_the_mac() -> None:
        job = await helper.claim()
        helper.finish(
            job["id"],
            {"base": BASE, "head": HEAD},
            branch=job["branch"],
            head="c0ffee",
            take_over="cd w && claude --resume s",
        )

    report, _ = await asyncio.gather(
        executor(client, tmp_path, Steps()).execute(scope, approved=True), on_the_mac()
    )
    with factory(client)() as session, session.begin():
        run = session.get(Run, UUID(run_id))
        assert run is not None
        session.add(
            Approval(
                run_id=run.id,
                task_id=run.root_task_id,
                action_type="engineering_scope",
                status="APPROVED",
                scope_json=scope.model_dump(mode="json"),
                scope_digest=scope.digest(),
                resolved_at=datetime.now(UTC),
            )
        )
        run.state_json = {**(run.state_json or {}), "engineering": report, "approval_id": "a"}
    return run_id, scope


async def test_work_in_the_owners_folder_can_be_merged_once_with_the_reviewed_commit(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, _ = await approved_task(client, tmp_path)
    card = (await client.get(f"/runs/{run_id}/coder")).json()
    assert (card["merge"], card["discard"], card["send_back"]) == (True, True, True)
    assert card["jobs"][0]["outcome"]["take_over"] == "cd w && claude --resume s"
    merged = await client.post(f"/runs/{run_id}/coder/merge")
    assert merged.status_code == 202
    helper = Helper(client, tmp_path)
    job = await helper.claim()
    assert (job["kind"], job["base_commit"]) == ("merge", "c0ffee")
    # While it merges, nothing else can be pressed.
    again = await client.post(f"/runs/{run_id}/coder/merge")
    assert again.status_code == 409
    helper.finish(job["id"], {}, branch=job["branch"], head="beef")
    settled = (await client.get(f"/runs/{run_id}/coder")).json()
    assert (settled["merge"], settled["discard"], settled["send_back"]) == (False, False, False)


async def test_a_build_lands_itself_so_it_has_no_merge(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, _ = await approved_task(client, tmp_path)
    from app.db.models import Approval

    with factory(client)() as session, session.begin():
        row = session.scalars(select(Approval).where(Approval.run_id == UUID(run_id))).first()
        row.scope_json = {**row.scope_json, "lands_on_branch": ""}
    card = (await client.get(f"/runs/{run_id}/coder")).json()
    assert (card["merge"], card["discard"], card["send_back"]) == (False, False, True)
    refused = await client.post(f"/runs/{run_id}/coder/merge")
    assert refused.status_code == 409 and refused.json()["detail"] == "merge_not_possible"


async def test_send_back_resumes_the_same_session_and_branch_and_checks_again(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    run_id, scope = await approved_task(client, tmp_path)
    with factory(client)() as session, session.begin():
        first = jobs.job_for(session, UUID(run_id), scope.digest())
        assert first is not None
        first.session = "s-1"
    helper = Helper(client, tmp_path)
    redone = a_tar({"README.md": "site\n", "check.sh": "test -f page.txt\n", "page.txt": "red\n"})

    async def on_the_mac() -> None:
        job = await helper.claim()
        assert job["session"] == "s-1" and job["branch"] == scope.lands_on_branch
        assert "Make it red" in job["brief"]
        helper.finish(job["id"], {"base": BASE, "head": redone}, branch=job["branch"])

    report, _ = await asyncio.gather(
        executor(client, tmp_path, Steps()).send_back(scope, "Make it red"), on_the_mac()
    )
    assert report["status"] == EngineeringStatus.COMPLETED
    assert "+red" in report["diff"]


class Reviewer:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.seen = fail, ""

    async def run(self, objective: str, context: Any, inputs: str = "") -> Any:
        from app.agents.base import AgentOutputError
        from app.schemas.agent_output import CodeReview

        self.seen = inputs
        if self.fail:
            raise AgentOutputError("critic", "invalid_json")
        return CodeReview(
            verdict="concerns",
            findings=[{"path": "page.txt", "issue": "Says hi, the task asked for a price."}],
            plain="Adds a page.",
        )


async def test_the_reviewer_reads_the_diff_and_its_findings_are_kept(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    from app.coder.review import reviewed

    run_id, scope = await approved_task(client, tmp_path)
    with factory(client)() as session:
        from app.db.models import Run

        report = dict(session.get(Run, UUID(run_id)).state_json["engineering"])
    reviewer = Reviewer()
    review = await reviewed(reviewer, scope, report, None)  # type: ignore[arg-type]
    assert review is not None and review["verdict"] == "concerns"
    assert "+hi" in reviewer.seen and "Acceptance criteria" in reviewer.seen
    quiet = await reviewed(Reviewer(fail=True), scope, report, None)  # type: ignore[arg-type]
    assert quiet is not None and quiet["verdict"] == "not_reviewed"
    failed = {**report, "status": EngineeringStatus.FAILED}
    assert await reviewed(Reviewer(), scope, failed, None) is None  # type: ignore[arg-type]


async def build_scope_for(client: httpx.AsyncClient, **given: Any) -> tuple[ApprovalScope, Path]:
    run_id, project_id = await a_run(client)
    settings = client.settings  # type: ignore[attr-defined]
    folder = builds.folder_for(settings, run_id)
    builds.ensure_repository(folder)
    fields: dict[str, Any] = {
        "repository": str(folder),
        "base_commit": git("rev-parse", "HEAD", where=folder),
        "lands_on_branch": "",
        "permitted_paths": list(builds.PERMITTED),
        "test_command": builds.CHECK_COMMAND,
    }
    return space_scope(run_id, project_id, **(fields | given)), folder


async def checked_build(client: httpx.AsyncClient, scope: ApprovalScope, head: bytes) -> Any:
    settings = client.settings  # type: ignore[attr-defined]
    helper = Helper(client, settings.engineering_root)

    async def on_the_mac() -> None:
        job = await helper.claim()
        helper.finish(job["id"], {"head": head}, branch=job["branch"])

    report, _ = await asyncio.gather(
        CoderHandoff(
            session_factory=factory(client),
            engineering_root=settings.engineering_root,
            test_executor=LocalExecutor(),
            poll_seconds=0.02,
        ).execute(scope, approved=True),
        on_the_mac(),
    )
    return report


async def test_a_build_whose_check_fails_leaves_its_folder_as_it_was(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    scope, folder = await build_scope_for(client)
    before = git("rev-parse", "HEAD", where=folder)
    report = await checked_build(client, scope, a_tar({"check.sh": "exit 1\n", "a.txt": "x"}))
    assert report["errors"] == ["tests_failed"]
    assert git("rev-parse", "HEAD", where=folder) == before
    assert git("status", "--porcelain", where=folder) == ""


async def test_a_check_that_writes_outside_the_approved_paths_is_refused(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    scope, folder = await build_scope_for(client, permitted_paths=["site/*", "check.sh"])
    head = a_tar({"site/index.html": "<p>", "check.sh": "echo sneaky > outside.txt\n"})
    report = await checked_build(client, scope, head)
    assert report["errors"] == ["out_of_scope_after_tests"]
    assert "outside.txt" in report["changed_files"]


async def test_a_sent_back_round_that_passes_makes_the_task_done_with_its_version(
    client: httpx.AsyncClient, no_dispatch: list[Any], tmp_path: Path
) -> None:
    from app.api.routers.coder import settled
    from app.db.models import Run

    run_id, _ = await a_run(client)
    with factory(client)() as session, session.begin():
        run = session.get(Run, UUID(run_id))
        assert run is not None
        run.status = "FAILED"
        settled(session, run, {"status": EngineeringStatus.COMPLETED, "commit": "abc123"})
    with factory(client)() as session:
        run = session.get(Run, UUID(run_id))
        assert run is not None and run.status == "COMPLETED" and run.error is None
        assert run.state_json["build_commit"] == "abc123"
    with factory(client)() as session, session.begin():
        run = session.get(Run, UUID(run_id))
        settled(session, run, {"status": EngineeringStatus.FAILED, "errors": ["tests_failed"]})
    with factory(client)() as session:
        run = session.get(Run, UUID(run_id))
        assert run is not None and run.status == "FAILED" and run.error == "engineering_failed"
