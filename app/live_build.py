"""A build as it is being written, for the owner to watch and steer.

The build runs in an attempt clone under the engineering folder; only the
finished version reaches the project folder. While it runs, this reads:

- its steps from the task's events, from this attempt on: the agent's plan,
  what it is doing now, each file with its change, its notes and reasoning,
  and the owner's own comments;
- the clone itself, for the live preview: whether it holds a website and when
  a file last changed, so the page reloads only when something did.

The clone is untrusted output. Its path comes from the office's own event and
must lie inside the engineering folder; its files are served only through the
sandboxed preview.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Run, RunControl, RunEvent
from app.runtimes.steering import KINDS

# Shown in the live view, in this order of arrival.
SHOWN = ("phase", "file", "command", "note", "thinking", "owner", "paused", "check")
STEPS_SHOWN = 80
SKIPPED_FOLDERS = {".git", "node_modules", "py_packages"}
MAX_FILES_SCANNED = 3000
SITE_FOLDERS = ("dist", "build", "public", "out", "_site")


def _events(session: Session, run_id: object) -> list[RunEvent]:
    return list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.kind == "build_step")
            .order_by(RunEvent.id)
        )
    )


def this_attempt(events: list[RunEvent]) -> list[RunEvent]:
    """The steps of the latest attempt: from its workspace on."""
    start = 0
    for index, event in enumerate(events):
        if (event.payload_json or {}).get("kind") == "workspace":
            start = index
    return events[start:]


def clone_of(events: list[RunEvent], engineering_root: Path) -> Path | None:
    """The attempt clone, only if it lies inside the engineering folder."""
    for event in reversed(events):
        payload = event.payload_json or {}
        if payload.get("kind") != "workspace":
            continue
        try:
            path = Path(str(payload.get("path") or "")).resolve()
            path.relative_to(engineering_root.resolve())
        except (ValueError, OSError):
            return None
        return path if path.is_dir() else None
    return None


def site_of(clone: Path) -> Path | None:
    """Where its website is, if it has one yet."""
    for name in SITE_FOLDERS:
        if (clone / name / "index.html").is_file():
            return clone / name
    return clone if (clone / "index.html").is_file() else None


def last_change(clone: Path) -> float:
    """When a file in the clone last changed (skipping git and packages)."""
    newest = 0.0
    seen = 0
    stack = [clone]
    while stack and seen < MAX_FILES_SCANNED:
        folder = stack.pop()
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIPPED_FOLDERS or entry.is_symlink():
                continue
            if entry.is_dir():
                stack.append(entry)
                continue
            seen += 1
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
    return newest


def live_view(
    session: Session, run: Run, engineering_root: Path, preview_base: str | None
) -> dict[str, Any]:
    """Everything the live card shows, read fresh each time it asks."""
    events = this_attempt(_events(session, run.id))
    plan: list[dict[str, Any]] = []
    now: str | None = None
    paused = False
    steps: list[dict[str, Any]] = []
    for event in events:
        payload = dict(event.payload_json or {})
        kind = payload.get("kind")
        if kind == "plan":
            plan = list(payload.get("items") or [])
        elif kind == "now":
            now = str(payload.get("text") or "")
        elif kind in ("command", "file", "note"):
            now = None
        if kind == "paused":
            paused = True
        elif kind == "phase":
            paused = False
        if kind in SHOWN:
            steps.append({"at": event.created_at.isoformat(), **payload})
    clone = clone_of(events, engineering_root)
    site = site_of(clone) if clone is not None else None
    # The clone is removed once the attempt ends, so a running task with a
    # clone is a build in progress.
    building = str(run.status) in ("RUNNING", "RECOVERING")
    notes = [
        {
            "id": str(row.id),
            "kind": row.kind,
            "text": row.text,
            "status": row.status,
            "at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in session.scalars(
            select(RunControl)
            .where(RunControl.run_id == run.id, RunControl.kind.in_(KINDS))
            .order_by(RunControl.created_at)
        )
    ]
    preview = None
    if site is not None and clone is not None and preview_base:
        inside = site.relative_to(clone).as_posix()
        preview = f"{preview_base}/live/{'' if inside == '.' else inside + '/'}index.html"
    return {
        "building": building and clone is not None,
        "began": events[0].created_at.isoformat() if events else None,
        "agent": next(
            (
                "Claude Code" if "Claude Code" in str(p.get("text")) else "Codex"
                for p in (e.payload_json or {} for e in events)
                if p.get("kind") == "phase" and "is writing it" in str(p.get("text"))
            ),
            "Codex",
        ),
        "plan": plan,
        "now": now,
        "paused": paused,
        "steps": steps[-STEPS_SHOWN:],
        "earlier": max(0, len(steps) - STEPS_SHOWN),
        "notes": notes,
        "preview": preview,
        "changed": last_change(clone) if clone is not None else 0.0,
    }


def pointed(given: dict[str, Any] | None) -> str:
    """What the owner pointed at on the live page, as words for the agent.

    It came from the build's own page, so it is untrusted: cut short and
    quoted, never followed as an instruction.
    """
    if not given:
        return ""
    tag = "".join(ch for ch in str(given.get("tag") or "")[:20] if ch.isalnum())
    text = " ".join(str(given.get("text") or "").split())[:160]
    where = " ".join(str(given.get("selector") or "").split())[:200]
    parts = [f"<{tag}>" if tag else "an element"]
    if text:
        parts.append(f"showing “{text}”")
    if where:
        parts.append(f"(at {where})")
    return "On the live page, the owner pointed at " + " ".join(parts) + "."
