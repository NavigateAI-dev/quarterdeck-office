"""The coder on the owner's Mac (Stage 36a): the helper's questions and reports,
and the owner's view of it.

The helper calls these with the owner token from the office's own `.env`, from
this Mac only. It asks; the office never reaches into the Mac.
"""

import subprocess  # noqa: S404 - fixed git command, no shell
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import builds, run_state
from app.api.routers.deps import RouterDeps
from app.api.schemas.base import StrictRequest
from app.approvals import ApprovalScope
from app.coder import jobs
from app.coder.handoff import CoderHandoff
from app.coder.observer import BuildObserver
from app.db.models import Approval, CoderJob, Run, RunEvent
from app.runtime.worker import build_executor
from app.schemas.engineering import EngineeringStatus

# A branch's files, as a tar from `git archive`: enough for a real project.
MOST_FILES_BYTES = 200 * 1024 * 1024


class CliFound(BaseModel):
    """One coding CLI the helper found on the Mac."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(max_length=32)
    path: str = Field(default="", max_length=500)
    version: str = Field(default="", max_length=200)
    signed_in: bool = False
    detail: str = Field(default="", max_length=500)


class HelperHello(StrictRequest):
    version: str = Field(default="", max_length=32)
    root: str = Field(default="", max_length=500)
    clis: list[CliFound] = Field(default_factory=list, max_length=10)


class JobLines(BaseModel):
    """The CLI's output since the last report, unchanged."""

    model_config = ConfigDict(extra="forbid")

    lines: list[str] = Field(default_factory=list, max_length=jobs.LINES_AT_ONCE)
    session: str = Field(default="", max_length=120)
    worktree: str = Field(default="", max_length=500)


class JobDone(StrictRequest):
    branch: str = Field(default="", max_length=255)
    head: str = Field(default="", max_length=64)
    base: str = Field(default="", max_length=64)
    exit_code: int | None = None
    message: str = Field(default="", max_length=jobs.MESSAGE_CHARS)
    error: str = Field(default="", max_length=64)
    take_over: str = Field(default="", max_length=1000)
    stopped: bool = False


class JobNote(StrictRequest):
    text: str = Field(min_length=1, max_length=jobs.NOTE_CHARS)
    now: bool = False


class SendBack(StrictRequest):
    notes: str = Field(min_length=1, max_length=jobs.NOTE_CHARS)


def approved_scope(session: Session, run_id: UUID) -> ApprovalScope | None:
    """The scope the owner approved for this task's coding."""
    row = session.scalars(
        select(Approval)
        .where(Approval.run_id == run_id, Approval.status == "APPROVED")
        .order_by(Approval.resolved_at.desc().nulls_last())
        .limit(1)
    ).first()
    return ApprovalScope.model_validate(row.scope_json) if row is not None else None


def choices(session: Session, run_id: UUID) -> dict[str, Any]:
    """The task's coder jobs and what the owner may do with the work now:
    Merge and Discard for work in their own folder (a build lands itself),
    Send back for either, never while a job is still going."""
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    rows = jobs.jobs_of(session, run_id)
    code = next((row for row in rows if row.kind == "code"), None)
    scope = approved_scope(session, run_id)
    report = dict((run.state_json or {}).get("engineering") or {})
    going = any(row.state in ("queued", *jobs.OPEN) for row in rows)
    after = [row for row in rows if code is not None and row.created_at >= code.created_at]
    settled = any(row.kind in ("merge", "discard") and row.state == "done" for row in after)
    own_folder = scope is not None and bool(scope.lands_on_branch)
    passed = report.get("status") == EngineeringStatus.COMPLETED
    closed = code is not None and code.state in (*jobs.CLOSED, "unknown")
    return {
        "jobs": [jobs.job_view(row) for row in rows[:10]],
        "merge": own_folder and passed and closed and not going and not settled,
        "discard": own_folder and closed and not going and not settled,
        "send_back": scope is not None and closed and not going and not settled,
        "reviewed_commit": report.get("commit") if passed else None,
        "code": code,
        "scope": scope,
    }


def settled(session: Session, run: Run, report: dict[str, Any]) -> None:
    """A sent-back round decides how the task now stands, as the first round
    did: done when its work passed the office's checks, failed when it did not,
    stopped when the owner stopped it. A build's new version is this task's."""
    values: dict[str, Any] = {"engineering": report}
    stopped = "runtime:stopped_by_owner" in (report.get("errors") or [])
    if report.get("status") == EngineeringStatus.COMPLETED:
        status, reason = "COMPLETED", None
        if report.get("commit"):
            values["build_commit"] = str(report["commit"])
    elif stopped:
        status, reason = "CANCELLED", "stopped_by_owner"
    else:
        status, reason = "FAILED", "engineering_failed"
    values |= {"status": status, "stopped_reason": reason}
    run_state.write(session, run, run_state.SET, values, "send_back")
    run.status = status
    run.error = reason
    run.completed_at = datetime.now(UTC)


def seed_archive(folder: Path, commit: str | None) -> bytes:
    """A build folder's files at the approved commit, as a tar; empty when new."""
    if not commit:
        return b""
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(folder), "archive", "--format=tar", commit],  # noqa: S607
        capture_output=True,
        timeout=120,
        check=False,
    )
    if done.returncode != 0:
        raise HTTPException(status_code=409, detail="seed_unavailable")
    return done.stdout


def _refused(error: jobs.JobRefused) -> HTTPException:
    status = 404 if error.code == "job_not_found" else 409
    return HTTPException(status_code=status, detail=error.code)


def add_coder_routes(router: APIRouter, d: RouterDeps) -> None:
    def now() -> datetime:
        return datetime.now(UTC)

    @router.get("/coder")
    def coder_view() -> dict[str, Any]:
        """The "Coder on this Mac" card."""
        with d.session_factory() as session:
            return jobs.helper_view(session, now())

    @router.post("/coder/hello")
    def helper_hello(body: HelperHello) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            return jobs.hello(session, body.model_dump(), now())

    @router.post("/coder/jobs/next")
    def next_job() -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            return {"job": jobs.claim_next(session, now())}

    @router.post("/coder/jobs/{job_id}/lines")
    def job_lines(job_id: UUID, body: JobLines) -> dict[str, Any]:
        try:
            with d.session_factory() as session, session.begin():
                return jobs.heard(session, job_id, body.lines, now(), body.session, body.worktree)
        except jobs.JobRefused as error:
            raise _refused(error) from error

    @router.post("/coder/jobs/{job_id}/done")
    def job_done(job_id: UUID, body: JobDone) -> dict[str, Any]:
        try:
            with d.session_factory() as session, session.begin():
                return jobs.finish(session, job_id, body.model_dump(), now())
        except jobs.JobRefused as error:
            raise _refused(error) from error

    @router.get("/coder/jobs/{job_id}")
    def job(job_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            row = session.get(CoderJob, job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="job_not_found")
            return jobs.job_view(row)

    @router.post("/coder/jobs/{job_id}/stop")
    def stop_job(job_id: UUID) -> dict[str, Any]:
        try:
            with d.session_factory() as session, session.begin():
                return jobs.ask_stop(session, job_id, now())
        except jobs.JobRefused as error:
            raise _refused(error) from error

    @router.post("/coder/jobs/{job_id}/notes")
    def note_job(job_id: UUID, body: JobNote) -> dict[str, Any]:
        try:
            with d.session_factory() as session, session.begin():
                return jobs.add_note(session, job_id, body.text, body.now)
        except jobs.JobRefused as error:
            raise _refused(error) from error

    @router.get("/coder/jobs/{job_id}/seed")
    def job_seed(job_id: UUID) -> Response:
        """A build's office version, for the helper to start its worktree from."""
        with d.session_factory() as session:
            row = session.get(CoderJob, job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="job_not_found")
            if row.state not in jobs.OPEN or not row.seed_from:
                raise HTTPException(status_code=409, detail="no_seed")
            folder, commit = Path(row.seed_from), row.base_commit
        root = builds.root_of(d.settings).resolve()
        if not folder.resolve().is_relative_to(root):
            raise HTTPException(status_code=409, detail="seed_outside_builds")
        return Response(seed_archive(folder, commit), media_type="application/x-tar")

    @router.put("/coder/jobs/{job_id}/files/{which}")
    async def job_files(
        job_id: UUID, which: Literal["head", "base"], request: Request
    ) -> dict[str, Any]:
        """The branch's files at its end (and, for the owner's own folder, at its
        start), from `git archive`, kept for the office's own checks."""
        with d.session_factory() as session:
            row = session.get(CoderJob, job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="job_not_found")
            if row.state not in jobs.OPEN:
                raise HTTPException(status_code=409, detail=f"job_{row.state}")
        folder = jobs.files_of(d.settings.engineering_root, job_id)
        folder.mkdir(parents=True, exist_ok=True)
        target, partial = folder / f"{which}.tar", folder / f"{which}.tar.part"
        size = 0
        with partial.open("wb") as written:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MOST_FILES_BYTES:
                    written.close()
                    partial.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="files_too_large")
                written.write(chunk)
        partial.replace(target)
        return {"kept": which, "bytes": size}

    def public(found: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in found.items() if key not in ("code", "scope")}

    @router.get("/runs/{run_id}/coder")
    def task_coder(run_id: UUID) -> dict[str, Any]:
        """The review card's jobs and buttons."""
        with d.session_factory() as session:
            return public(choices(session, run_id))

    def settle(run_id: UUID, kind: str) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            found = choices(session, run_id)
            code = found["code"]
            if not found[kind] or code is None:
                raise HTTPException(status_code=409, detail=f"{kind}_not_possible")
            try:
                job = jobs.queue_job(
                    session,
                    run_id=run_id,
                    kind=kind,
                    folder=code.folder,
                    branch=code.branch,
                    cli=code.cli,
                    brief="",
                    base_branch=code.base_branch,
                    # A merge carries the commit the office checked and reviewed.
                    base_commit=found["reviewed_commit"] if kind == "merge" else None,
                )
            except jobs.JobRefused as error:
                raise _refused(error) from error
            pressed = "You pressed Merge." if kind == "merge" else "You pressed Discard."
            session.add(
                RunEvent(
                    run_id=run_id,
                    kind="build_step",
                    payload_json={"kind": "owner", "text": pressed},
                )
            )
            return jobs.job_view(job)

    @router.post("/runs/{run_id}/coder/merge", status_code=202)
    def merge_work(run_id: UUID) -> dict[str, Any]:
        """The owner's Merge, carried out by the helper on the Mac."""
        return settle(run_id, "merge")

    @router.post("/runs/{run_id}/coder/discard", status_code=202)
    def discard_work(run_id: UUID) -> dict[str, Any]:
        return settle(run_id, "discard")

    @router.post("/runs/{run_id}/coder/send-back", status_code=202)
    def send_back(run_id: UUID, body: SendBack, background: BackgroundTasks) -> dict[str, Any]:
        """Back to the same CLI session with the owner's notes; checked again."""
        with d.session_factory() as session:
            found = choices(session, run_id)
        if not found["send_back"] or found["scope"] is None:
            raise HTTPException(status_code=409, detail="send_back_not_possible")
        background.add_task(sent_back, run_id, found["scope"], body.notes)
        return {"sent_back": True}

    async def sent_back(run_id: UUID, scope: ApprovalScope, notes: str) -> None:
        executor = build_executor(d.settings, d.session_factory)
        if not isinstance(executor, CoderHandoff):
            return
        repository = d.repository

        def said(item: dict[str, Any]) -> None:
            repository.add_event(run_id, "build_step", dict(item))

        executor.observer = BuildObserver(
            step=said,
            stop=lambda: (repository.pending_stop(run_id) or {}).get("kind") == "cancel",
            notes=lambda: repository.pending_build_notes(run_id),
            take=lambda ids: repository.take_build_notes(run_id, ids),
        )
        said({"kind": "owner", "text": f"You sent it back: {notes[:300]}"})
        try:
            report = await executor.send_back(scope, notes)
        except jobs.JobRefused as refused:
            said(
                {"kind": "phase", "text": f"Could not send it back ({refused.code}).", "ok": False}
            )
            return
        report["review"] = {
            "verdict": "not_reviewed",
            "findings": [],
            "plain": "Sent back: checked again by the office, not read again by the reviewer.",
        }
        with d.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is not None:
                settled(session, run, report)
