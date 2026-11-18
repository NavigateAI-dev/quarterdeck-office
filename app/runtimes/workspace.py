import shutil
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from uuid import UUID

from app.runtimes.base import ExecutionError, ExecutionLimits, run_bounded

GIT = "git"
BRANCH_PREFIX = "quarterdeck/attempt-"


@dataclass(frozen=True)
class Workspace:
    """An isolated clone of a repository for one engineering attempt."""

    root: Path
    repository: Path
    base_commit: str
    branch: str = ""


class ScopeViolation(Exception):
    """A change fell outside the approved permitted paths."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"out_of_scope:{','.join(sorted(paths)[:5])}")


def within(root: Path, candidate: Path) -> bool:
    """True when candidate resolves inside root, following symlinks."""
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved == root.resolve() or root.resolve() in resolved.parents


def path_allowed(path: str, permitted: list[str]) -> bool:
    """Match a repository-relative path against approved glob patterns.

    An empty permitted list allows nothing. An approval that named no paths
    authorizes no file changes, which is the safe reading.
    """
    if ".." in Path(path).parts or path.startswith("/"):
        return False
    return any(fnmatch(path, pattern) for pattern in permitted)


async def create_workspace(
    source: Path, root: Path, attempt_id: UUID, base_commit: str | None = None
) -> Workspace:
    """Clone a repository into an attempt-specific directory under root.

    The clone is the isolation boundary for changes, not a security boundary:
    the process restrictions are what prevent reaching anything else.
    """
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / str(attempt_id)).resolve()
    if not within(root, destination):
        raise ExecutionError("workspace_escapes_root")
    if destination.exists():
        shutil.rmtree(destination)
    clone = await run_bounded(
        [GIT, "clone", "--no-hardlinks", "--quiet", str(source), str(destination)],
        root,
        ExecutionLimits(timeout_seconds=120),
    )
    if not clone.succeeded:
        raise ExecutionError("clone_failed", clone.stderr[:200])
    # SPEC 8.5 requires an isolated branch. Work never lands on the base branch,
    # so an attempt cannot alter the branch it was derived from. It starts at
    # the approved commit, whichever branch the source has checked out.
    branch = f"{BRANCH_PREFIX}{attempt_id}"
    start = [base_commit] if base_commit else []
    checkout = await run_bounded([GIT, "checkout", "--quiet", "-b", branch, *start], destination)
    if not checkout.succeeded:
        code = "base_commit_unavailable" if base_commit else "branch_creation_failed"
        raise ExecutionError(code, checkout.stderr[:200])
    head = await run_bounded([GIT, "rev-parse", "HEAD"], destination)
    if not head.succeeded:
        raise ExecutionError("base_commit_unresolved")
    return Workspace(
        root=root,
        repository=destination,
        base_commit=head.stdout.strip(),
        branch=branch,
    )


async def changed_paths(workspace: Workspace) -> list[str]:
    """Repository-relative paths changed since the base commit.

    Compares the working tree with the approved base, so committed, staged,
    unstaged and untracked changes are all included. Renames are split into a
    deletion and an addition, so both the old and the new path are checked.
    """
    added = await run_bounded([GIT, "add", "--intent-to-add", "--all"], workspace.repository)
    if not added.succeeded:
        raise ExecutionError("status_failed")
    result = await run_bounded(
        [GIT, "diff", "--name-only", "--no-renames", "-z", workspace.base_commit],
        workspace.repository,
    )
    if not result.succeeded:
        raise ExecutionError("status_failed")
    return sorted({path for path in result.stdout.split("\0") if path})


async def diff(workspace: Workspace) -> str:
    """The full diff against the base commit, including untracked content."""
    await run_bounded([GIT, "add", "--intent-to-add", "--all"], workspace.repository)
    result = await run_bounded(
        [GIT, "diff", "--no-color", "--no-renames", workspace.base_commit],
        workspace.repository,
    )
    if not result.succeeded:
        raise ExecutionError("diff_failed")
    return result.stdout


async def write_patch(workspace: Workspace, target: Path) -> int:
    """The full diff against the base commit, written straight to a file.

    Git writes it itself, so it never passes through the capped output of a
    bounded command. Returns its size; raises if it could not be written.
    """
    await run_bounded([GIT, "add", "--intent-to-add", "--all"], workspace.repository)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = await run_bounded(
        [
            GIT,
            "diff",
            "--no-color",
            "--no-renames",
            "--binary",
            f"--output={target}",
            workspace.base_commit,
        ],
        workspace.repository,
    )
    if not result.succeeded or not target.is_file():
        raise ExecutionError("patch_not_written")
    return target.stat().st_size


async def assert_within_scope(workspace: Workspace, permitted: list[str]) -> list[str]:
    """Verify every change falls inside the approved paths.

    The check is on the actual diff rather than on what the runtime reports,
    because the runtime's own account of its work is not evidence.
    """
    changed = await changed_paths(workspace)
    violations = [path for path in changed if not path_allowed(path, permitted)]
    if violations:
        raise ScopeViolation(violations)
    return changed


async def current_branch(workspace: Workspace) -> str:
    result = await run_bounded([GIT, "rev-parse", "--abbrev-ref", "HEAD"], workspace.repository)
    return result.stdout.strip() if result.succeeded else ""


async def base_commit_unchanged(workspace: Workspace, expected: str) -> bool:
    """A different base means the approved scope was resolved against other code."""
    head = await run_bounded([GIT, "rev-parse", "HEAD"], workspace.repository)
    return head.succeeded and head.stdout.strip() == expected


def discard(workspace: Workspace) -> None:
    if within(workspace.root, workspace.repository):
        shutil.rmtree(workspace.repository, ignore_errors=True)
