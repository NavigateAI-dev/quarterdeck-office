"""The office's own project folders for what it builds.

A build gets a git repository of its own in the engineering volume, made the
first time it is needed. A follow-up ("make the buttons bigger") continues in
the folder of the build it follows, so the work accumulates in one place. The
coding agent works on a clone; only finished, checked work is committed here.
"""

from __future__ import annotations

import io
import subprocess  # noqa: S404 - fixed executable, no shell
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Run

BUILDS = "builds"
# The check the coding agent writes and the office runs, isolated.
CHECK_COMMAND = "sh check.sh"
PERMITTED = ["*"]
IDENTITY = ["-c", "user.name=Quarterdeck", "-c", "user.email=office@localhost"]
# What the file view leaves out.
SKIP = {".git", "node_modules", "py_packages", "__pycache__", ".venv", "dist-cache"}
MAX_FILES = 400
MAX_SHOWN_BYTES = 200_000
MAX_ZIP_BYTES = 50_000_000


class BuildError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def root_of(settings: Any) -> Path:
    return Path(settings.engineering_root) / BUILDS


def folder_for(settings: Any, key: str) -> Path:
    return root_of(settings) / key


def project_key(session: Session, run_id: UUID) -> str:
    """The folder a build works in: the one of the build it follows, if any."""
    seen: set[UUID] = set()
    run = session.get(Run, run_id)
    parent = run.continues_run_id if run is not None else None
    while parent is not None and parent not in seen:
        seen.add(parent)
        before = session.get(Run, parent)
        if before is None:
            break
        if (before.state_json or {}).get("build_folder"):
            return str((before.state_json or {})["build_folder"])
        parent = before.continues_run_id
    return str(run_id)


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def ensure_repository(path: Path) -> None:
    """A git repository with one empty commit on main, made once."""
    if (path / ".git").is_dir():
        return
    path.mkdir(parents=True, exist_ok=True)
    if _git(path, "init", "-q", "-b", "main").returncode != 0:
        raise BuildError("build_folder_not_made")
    # Installed packages are never part of what a build saves.
    (path / ".gitignore").write_text("node_modules/\npy_packages/\n")
    _git(path, "add", ".gitignore")
    made = _git(path, *IDENTITY, "commit", "-q", "-m", "Start")
    if made.returncode != 0:
        raise BuildError("build_folder_not_made")


def _inside(root: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    base = root.resolve()
    return resolved == base or base in resolved.parents


def files(path: Path) -> list[dict[str, Any]]:
    """What the build holds, for the owner to read: path and size."""
    if not path.is_dir():
        return []
    found = []
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if any(part in SKIP for part in relative.parts) or not item.is_file():
            continue
        if not _inside(path, item):
            continue  # A link out of the folder is not shown.
        found.append({"path": relative.as_posix(), "bytes": item.stat().st_size})
        if len(found) >= MAX_FILES:
            break
    return found


def read_file(path: Path, relative: str) -> tuple[bytes, Path]:
    """One file of the build, only from inside its folder."""
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise BuildError("file_not_found")
    target = path / relative
    if any(part in SKIP for part in Path(relative).parts):
        raise BuildError("file_not_found")
    if not _inside(path, target) or not target.is_file():
        raise BuildError("file_not_found")
    return target.read_bytes(), target


def zipped(path: Path, name: str) -> bytes:
    """The build as a zip, under one folder named for it."""
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in files(path):
            data, _ = read_file(path, entry["path"])
            total += len(data)
            if total > MAX_ZIP_BYTES:
                raise BuildError("build_too_large")
            archive.writestr(f"{name}/{entry['path']}", data)
    return buffer.getvalue()


def history(path: Path, limit: int = 20) -> list[dict[str, str]]:
    """The builds committed into this folder, newest first."""
    if not (path / ".git").is_dir():
        return []
    log = _git(path, "log", f"-{limit}", "--format=%H%x1f%cI%x1f%s")
    if log.returncode != 0:
        return []
    rows = []
    for line in log.stdout.splitlines():
        commit, when, subject = (line.split("\x1f") + ["", "", ""])[:3]
        rows.append({"commit": commit[:12], "at": when, "subject": subject})
    return rows


def preview_token(secret: str, run_id: UUID) -> str:
    """The private link a build's preview is reached by.

    The preview runs the build's own code, so it is served sandboxed and
    cannot carry the owner's session; the link is what lets it in instead.
    """
    import hashlib
    import hmac

    return hmac.new(secret.encode(), f"preview:{run_id}".encode(), hashlib.sha256).hexdigest()[:32]


# What a preview may do: run its scripts and print, never reach the office.
PREVIEW_POLICY = (
    "sandbox allow-scripts allow-modals allow-popups allow-forms; "
    "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'none'; "
    "frame-ancestors 'self'"
)


# ---- Versions ----
# Each finished build is a commit in its project folder. A version is never
# overwritten: restoring an old one makes a new version with its files.

CHANGES = {"A": "added", "M": "edited", "D": "removed"}


def _git_bytes(path: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        capture_output=True,
        timeout=120,
        check=False,
    )


def versions(path: Path) -> list[dict[str, Any]]:
    """Every version of the build, oldest first, numbered from 1."""
    if not (path / ".git").is_dir():
        return []
    log = _git(
        path,
        "log",
        "--reverse",
        "--format=%H%x1f%cI%x1f%s%x1f%(trailers:key=Office-Run,valueonly,separator=)%x1e",
    )
    if log.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for record in log.stdout.split("\x1e"):
        parts = record.strip().split("\x1f")
        if len(parts) < 3 or parts[2] == "Start":
            continue
        commit, when, subject = parts[0], parts[1], parts[2]
        made_by = parts[3].strip() if len(parts) > 3 else ""
        shown = _git(path, "show", "--name-status", "--no-renames", "--format=", commit)
        changed = []
        for line in shown.stdout.splitlines():
            kind, _, file = line.partition("\t")
            if file and file != ".gitignore":
                changed.append({"change": CHANGES.get(kind[:1], "edited"), "path": file})
        rows.append(
            {
                "number": len(rows) + 1,
                "commit": commit,
                "at": when,
                "subject": subject,
                "run_id": made_by or None,
                "changed": changed,
            }
        )
    if rows:
        rows[-1]["latest"] = True
    return rows


def version_of(path: Path, commit: str) -> dict[str, Any]:
    """One of the build's own versions, by its commit or a prefix of it.

    Only a version in the list is accepted: nothing else in git is reachable.
    """
    wanted = (commit or "").strip().lower()
    if len(wanted) < 7 or not all(c in "0123456789abcdef" for c in wanted):
        raise BuildError("version_not_found")
    for row in versions(path):
        if str(row["commit"]).startswith(wanted):
            return row
    raise BuildError("version_not_found")


def files_at(path: Path, commit: str) -> list[dict[str, Any]]:
    listed = _git(path, "ls-tree", "-r", "-l", commit)
    found = []
    for line in listed.stdout.splitlines():
        meta, _, file = line.partition("\t")
        parts = meta.split()
        if not file or any(part in SKIP for part in Path(file).parts):
            continue
        size = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        found.append({"path": file, "bytes": size})
    return found[:MAX_FILES]


def read_at(path: Path, commit: str, relative: str) -> bytes:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise BuildError("file_not_found")
    shown = _git_bytes(path, "show", f"{commit}:{relative}")
    if shown.returncode != 0:
        raise BuildError("file_not_found")
    return shown.stdout


def zipped_at(path: Path, commit: str, name: str) -> bytes:
    archive = _git_bytes(path, "archive", "--format=zip", f"--prefix={name}/", commit)
    if archive.returncode != 0:
        raise BuildError("version_not_found")
    if len(archive.stdout) > MAX_ZIP_BYTES:
        raise BuildError("build_too_large")
    return archive.stdout


def export_at(path: Path, commit: str, target: Path) -> Path:
    """The version's files in a folder of their own, for publishing it."""
    import tarfile

    archive = _git_bytes(path, "archive", "--format=tar", commit)
    if archive.returncode != 0:
        raise BuildError("version_not_found")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(target, filter="data")
    return target


def restore(path: Path, commit: str, by_run: UUID, number: int) -> str:
    """A new version with an old version's files; nothing is overwritten."""
    tree = _git(path, "rev-parse", f"{commit}^{{tree}}")
    head = _git(path, "rev-parse", "HEAD")
    if tree.returncode != 0 or head.returncode != 0:
        raise BuildError("version_not_found")
    made = _git(
        path,
        *IDENTITY,
        "commit-tree",
        tree.stdout.strip(),
        "-p",
        head.stdout.strip(),
        "-m",
        f"Restore version {number}",
        "-m",
        f"Office-Run: {by_run}",
    )
    if made.returncode != 0:
        raise BuildError("restore_failed")
    new = made.stdout.strip()
    moved = _git(path, "merge", "--ff-only", "-q", new)
    if moved.returncode != 0:
        raise BuildError("restore_failed")
    return new
