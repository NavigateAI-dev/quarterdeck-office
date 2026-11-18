"""The jobs the office hands the coder on the owner's Mac, and the helper itself.

The helper asks; the office never reaches into the Mac. Each job is handed out
once (`claim_next` locks the row), reported on while it runs (`heard`, which
also answers with the owner's Stop and comments), and closed once (`finish`).
A claimed job the helper stops reporting on becomes `unknown` after ten
minutes and is never handed out again: the work may or may not have happened,
and the owner decides, as with an engineering outcome the office could not see.

The office checks a job's folder and branch here before it is stored; the
helper checks them again on the Mac, because only it touches the files.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.coder.parsers import steps_of
from app.coding_names import AGENTS
from app.db.models import CoderHelper, CoderJob, RunEvent
from app.workspaces import refusal

# The coding CLIs the helper may find, by key and by the name the owner reads.
CLIS = AGENTS
# The ones the helper knows how to run headless (scripts/mac_coder.py).
RUNS = ("claude_code", "codex", "opencode", "dsh")
# code: write it; merge: the owner's Merge, into the base branch on the Mac;
# discard: the owner's Discard, its worktree and branch removed (36c).
KINDS = ("code", "merge", "discard")
BRANCH_PREFIX = "office/"
# Asked every few seconds; a minute without a word and it is shown as away.
HELPER_GONE = timedelta(seconds=60)
# A claimed job not heard from this long is unknown, never handed out again.
SILENT = timedelta(minutes=10)
OPEN = ("claimed", "running")
CLOSED = ("done", "failed", "cancelled")
LINES_AT_ONCE = 500
LINE_CHARS = 20_000
NOTE_CHARS = 4_000
MESSAGE_CHARS = 8_000


class JobRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- the helper ----


def _helper(session: Session) -> CoderHelper | None:
    return session.scalar(select(CoderHelper).limit(1))


def _seen(session: Session, now: datetime) -> CoderHelper:
    row = _helper(session)
    if row is None:
        row = CoderHelper(only=True, clis_json=[])
        session.add(row)
    row.seen_at = now
    return row


def _cli_of(given: Any) -> dict[str, Any] | None:
    if not isinstance(given, dict) or given.get("id") not in CLIS:
        return None
    return {
        "id": given["id"],
        "label": CLIS[given["id"]],
        "path": str(given.get("path") or "")[:500],
        "version": str(given.get("version") or "")[:80],
        "signed_in": bool(given.get("signed_in")),
        "detail": str(given.get("detail") or "")[:200],
    }


def hello(session: Session, report: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The helper's question, every few seconds: here is what I found; is there work?"""
    row = _seen(session, now)
    row.version = str(report.get("version") or "")[:32]
    row.root = str(report.get("root") or "")[:500]
    clis = [_cli_of(given) for given in report.get("clis") or []]
    row.clis_json = [cli for cli in clis if cli is not None]
    session.flush()
    _expire_silent(session, now)
    waiting = session.scalar(select(CoderJob.id).where(CoderJob.state == "queued").limit(1))
    return {"jobs": waiting is not None}


def connected(session: Session, now: datetime) -> bool:
    row = _helper(session)
    return row is not None and row.seen_at is not None and now - row.seen_at < HELPER_GONE


def helper_view(session: Session, now: datetime) -> dict[str, Any]:
    """The "Coder on this Mac" card: whether it is there, and what it found."""
    row = _helper(session)
    return {
        "connected": connected(session, now),
        "seen_at": row.seen_at.isoformat() if row is not None and row.seen_at else None,
        "version": row.version if row is not None else "",
        "root": row.root if row is not None else "",
        "clis": list(row.clis_json or []) if row is not None else [],
        "start": "cd ~/dev/quarterdeck && .venv/bin/python -m scripts.mac_coder start",
    }


# ---- jobs ----


def branch_refusal(branch: str) -> str | None:
    """Why the helper may not make this branch, or None when it may."""
    if not branch.startswith(BRANCH_PREFIX) or len(branch) > 255:
        return "branch_not_office"
    name = branch[len(BRANCH_PREFIX) :]
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        return "branch_not_office"
    if any(char in branch for char in " ~^:?*[\\") or branch.endswith((".lock", "/", ".")):
        return "branch_not_office"
    return None


def queue_job(
    session: Session,
    *,
    folder: str,
    branch: str,
    cli: str,
    brief: str,
    base_branch: str = "main",
    base_commit: str | None = None,
    run_id: UUID | None = None,
    kind: str = "code",
    scope_digest: str = "",
    seed_from: str = "",
) -> CoderJob:
    """A job for the helper, checked before it is stored."""
    folder = str(PurePosixPath(folder.strip())) if folder.strip() else ""
    refused = refusal(folder) or branch_refusal(branch)
    if refused:
        raise JobRefused(refused)
    if kind not in KINDS:
        raise JobRefused("job_kind_unknown")
    if cli not in CLIS:
        raise JobRefused("cli_unknown")
    if kind == "code" and not brief.strip():
        raise JobRefused("brief_empty")
    if kind == "merge" and not base_commit:
        # A merge carries the reviewed commit; the helper refuses a branch moved since.
        raise JobRefused("merge_needs_the_reviewed_commit")
    job = CoderJob(
        run_id=run_id,
        kind=kind,
        folder=folder,
        base_branch=base_branch or "main",
        base_commit=base_commit,
        branch=branch,
        cli=cli,
        brief=brief,
        state="queued",
        notes_json=[],
        outcome_json={},
        scope_digest=scope_digest[:64],
        seed_from=seed_from[:500],
    )
    session.add(job)
    session.flush()
    return job


def _expire_silent(session: Session, now: datetime) -> None:
    for job in session.scalars(
        select(CoderJob)
        .where(CoderJob.state.in_(OPEN), CoderJob.heard_at < now - SILENT)
        .with_for_update(skip_locked=True)
    ):
        job.state = "unknown"
        _said(session, job, {"kind": "phase", "text": UNKNOWN_SAID, "ok": False})


UNKNOWN_SAID = (
    "The coder on your Mac stopped reporting on this. It is not started again: "
    "look in its worktree to see how far it got."
)


def claim_next(session: Session, now: datetime) -> dict[str, Any] | None:
    """The oldest waiting job, handed out once; None when there is none."""
    _seen(session, now)
    _expire_silent(session, now)
    job = session.scalar(
        select(CoderJob)
        .where(CoderJob.state == "queued")
        .order_by(CoderJob.created_at, CoderJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.state = "claimed"
    job.claimed_at = now
    job.heard_at = now
    session.flush()
    took = {"code": f"{CLIS[job.cli]} on your Mac took it.", "merge": "Merging on your Mac."}
    _said(session, job, {"kind": "phase", "text": took.get(job.kind, "Discarding on your Mac.")})
    return handed(job)


def handed(job: CoderJob) -> dict[str, Any]:
    """What the helper is given: everything it needs, nothing else."""
    return {
        "id": str(job.id),
        "kind": job.kind,
        "folder": job.folder,
        "base_branch": job.base_branch,
        "base_commit": job.base_commit,
        "branch": job.branch,
        "cli": job.cli,
        "brief": job.brief,
        "session": job.session,
        # A build starts from the office's own version, which the helper fetches;
        # other work starts from the owner's branch, whose files it sends back too.
        "seed": bool(job.seed_from),
    }


def _open_job(session: Session, job_id: UUID) -> CoderJob:
    job = session.get(CoderJob, job_id, with_for_update=True)
    if job is None:
        raise JobRefused("job_not_found")
    if job.state not in OPEN:
        raise JobRefused(f"job_{job.state}")
    return job


def heard(
    session: Session,
    job_id: UUID,
    lines: list[str],
    now: datetime,
    cli_session: str = "",
    worktree: str = "",
) -> dict[str, Any]:
    """The CLI's latest output; answered with the owner's Stop and comments.

    Comments are handed over once: the helper gives them to the CLI as its
    next turn."""
    _seen(session, now)
    job = _open_job(session, job_id)
    job.state = "running"
    job.heard_at = now
    if cli_session and not job.session:
        job.session = cli_session[:120]
    if worktree and not job.worktree:
        refused = refusal(worktree)
        if refused:
            raise JobRefused(f"worktree_{refused}")
        job.worktree = worktree
    # The CLI names files by their place on the Mac.
    helper = _helper(session)
    root = Path(helper.root if helper is not None else "/") / (job.worktree or job.folder)
    kept = [str(line)[:LINE_CHARS] for line in lines[:LINES_AT_ONCE]]
    for step in steps_of(job.cli, kept, root):
        if step.get("kind") == "session" and not job.session:
            job.session = str(step.get("id") or "")[:120]
        _said(session, job, step)
    notes = list(job.notes_json or [])
    now_too = bool(job.notes_now and notes)
    job.notes_json = []
    job.notes_now = False
    session.flush()
    return {"stop": job.stop_asked, "notes": notes, "now": now_too}


def _outcome(given: dict[str, Any]) -> dict[str, Any]:
    def text(key: str, limit: int = 255) -> str:
        return str(given.get(key) or "")[:limit]

    code = given.get("exit_code")
    return {
        "branch": text("branch"),
        "head": text("head", 64),
        "base": text("base", 64),
        "exit_code": code if isinstance(code, int) else None,
        "message": text("message", MESSAGE_CHARS),
        "error": text("error", 64),
        "take_over": text("take_over", 1000),
        "stopped": bool(given.get("stopped")),
    }


def finish(session: Session, job_id: UUID, given: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The helper's last word on a job. A job reported after it went unknown
    keeps the report, since it says what happened; a closed job refuses."""
    _seen(session, now)
    job = session.get(CoderJob, job_id, with_for_update=True)
    if job is None:
        raise JobRefused("job_not_found")
    if job.state in CLOSED or job.state == "queued":
        raise JobRefused(f"job_{job.state}")
    outcome = _outcome(given)
    if outcome["branch"] and outcome["branch"] != job.branch:
        raise JobRefused("branch_changed")
    job.outcome_json = outcome
    job.finished_at = now
    job.heard_at = now
    if outcome["stopped"]:
        job.state = "cancelled"
    elif outcome["error"] or outcome["exit_code"] not in (0, None):
        job.state = "failed"
    else:
        job.state = "done"
    said = closing(job, outcome)
    _said(session, job, said)
    session.flush()
    return job_view(job)


# Why a merge or discard on the Mac did not happen, as the owner reads it.
WHY = {
    "branch_moved": "the branch changed after it was reviewed",
    "checkout_not_clean": "your checkout has uncommitted changes",
    "checkout_not_on_base": "your checkout is on another branch",
    "merge_conflict": "it conflicts with your branch; nothing was changed",
    "folder_not_found": "the folder is not there",
}


def closing(job: CoderJob, outcome: dict[str, Any]) -> dict[str, Any]:
    """What the task shows when a job ends."""
    why = WHY.get(outcome["error"], outcome["error"] or f"exit {outcome['exit_code']}")
    if job.kind == "merge":
        if job.state == "done":
            text = f"Merged {job.branch} into {job.base_branch} in {job.folder}, on your Mac."
            return {"kind": "phase", "text": text, "ok": True}
        return {"kind": "phase", "text": f"Not merged: {why}.", "ok": False}
    if job.kind == "discard":
        if job.state == "done":
            return {
                "kind": "phase",
                "text": f"Discarded {job.branch} and its worktree.",
                "ok": True,
            }
        return {"kind": "phase", "text": f"Not discarded: {why}.", "ok": False}
    if job.state == "cancelled":
        return {"kind": "phase", "text": "Stopped on your Mac, as you asked.", "ok": False}
    if job.state == "failed":
        return {
            "kind": "phase",
            "text": f"{CLIS[job.cli]} stopped with an error: {why}.",
            "ok": False,
        }
    return {"kind": "phase", "text": f"{CLIS[job.cli]} finished on {job.branch}.", "ok": True}


def ask_stop(session: Session, job_id: UUID, now: datetime) -> dict[str, Any]:
    """The owner's Stop: a waiting job never starts; a running one is stopped
    by the helper at its next report."""
    job = session.get(CoderJob, job_id, with_for_update=True)
    if job is None:
        raise JobRefused("job_not_found")
    if job.state == "queued":
        job.state = "cancelled"
        job.finished_at = now
    elif job.state in OPEN:
        job.stop_asked = True
    session.flush()
    return job_view(job)


def add_note(session: Session, job_id: UUID, text: str, now: bool = False) -> dict[str, Any]:
    """The owner's comment, for the CLI's next turn; `now` interrupts the turn."""
    if not text.strip():
        raise JobRefused("note_empty")
    job = session.get(CoderJob, job_id, with_for_update=True)
    if job is None:
        raise JobRefused("job_not_found")
    if job.state not in ("queued", *OPEN):
        raise JobRefused(f"job_{job.state}")
    job.notes_json = [*(job.notes_json or []), text.strip()[:NOTE_CHARS]]
    job.notes_now = job.notes_now or now
    session.flush()
    return job_view(job)


def jobs_of(session: Session, run_id: UUID) -> list[CoderJob]:
    """A task's jobs, the newest first."""
    return list(
        session.scalars(
            select(CoderJob)
            .where(CoderJob.run_id == run_id)
            .order_by(CoderJob.created_at.desc(), CoderJob.id)
        )
    )


def job_for(session: Session, run_id: UUID, scope_digest: str) -> CoderJob | None:
    """The code job carrying out this approval, if one was queued."""
    return session.scalar(
        select(CoderJob)
        .where(
            CoderJob.run_id == run_id,
            CoderJob.scope_digest == scope_digest,
            CoderJob.kind == "code",
        )
        .order_by(CoderJob.created_at.desc())
        .limit(1)
    )


def work_possible(session: Session, writable: bool, now: datetime) -> bool:
    """Whether a space's folder may be given work access: the helper is there
    to do the work on the Mac (or, as before, the office has a writable mount)."""
    return writable or connected(session, now)


def files_of(engineering_root: Path, job_id: UUID) -> Path:
    """Where a job's files are kept on the office's side: what the helper sent."""
    return Path(engineering_root) / "coder" / str(job_id)


def job_view(job: CoderJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "run_id": str(job.run_id) if job.run_id else None,
        "kind": job.kind,
        "folder": job.folder,
        "branch": job.branch,
        "cli": job.cli,
        "cli_label": CLIS.get(job.cli, job.cli),
        "state": job.state,
        "stop_asked": job.stop_asked,
        "worktree": job.worktree,
        "session": job.session,
        "outcome": dict(job.outcome_json or {}),
    }


def _said(session: Session, job: CoderJob, step: dict[str, Any]) -> None:
    """A step on the task's own live view, where the job belongs to a task."""
    if job.run_id is None:
        return
    session.add(RunEvent(run_id=job.run_id, kind="build_step", payload_json=dict(step)))
