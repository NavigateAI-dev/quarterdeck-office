"""Work access in a space's folder: branches the office writes, merges the owner makes
Off until the owner turns on a writable mount.

Coding work never touches the owner's checkout. It runs in the office's own
clone of the repository, as every coding attempt does (`app/runtimes/
workspace.py`), and when its checks pass the result is written into the
owner's repository as a branch `office/<thread>`: a ref, not a change to any
working tree. The default branch is never written by the office's work.

Merging that branch into the default branch is the owner's own act, one at a
time (`merge_workspace_branch`): fast-forward only, so nothing is ever
overwritten. When the default branch is checked out, the checkout must be clean
and is moved by `git merge --ff-only`; otherwise only the branch ref moves.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.runtimes.base import ExecutionError, ExecutionLimits, run_bounded

PREFIX = "office/"
SLUG = re.compile(r"[^a-z0-9]+")
LIMITS = ExecutionLimits(timeout_seconds=60)


class BranchRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def branch_for(title: str, key: str) -> str:
    """`office/<words of the thread>-<short id>`: readable, and one per thread."""
    words = SLUG.sub("-", title.lower()).strip("-")[:40].strip("-") or "work"
    tail = SLUG.sub("", key.lower())[:6] or "x"
    return f"{PREFIX}{words}-{tail}"


def _ours(branch: str) -> None:
    """The office writes only its own branches."""
    if not branch.startswith(PREFIX) or ".." in branch or branch.endswith("/"):
        raise BranchRefused("not_an_office_branch")


async def _git(args: list[str], where: Path) -> str:
    try:
        done = await run_bounded(["git", *args], where, LIMITS)
    except ExecutionError as failed:
        raise BranchRefused("git_failed") from failed
    if not done.succeeded:
        raise BranchRefused("git_failed")
    return str(done.stdout).strip()


async def land_on_branch(clone: Path, clone_branch: str, source: Path, branch: str) -> str:
    """Write the clone's finished branch into the owner's repository as `branch`.

    Only the ref is written, never a working tree; a later attempt in the same
    thread moves the same branch. Returns the commit it points at."""
    _ours(branch)
    await _git(["fetch", "-q", str(clone), f"+{clone_branch}:refs/heads/{branch}"], source)
    return await _git(["rev-parse", f"refs/heads/{branch}"], source)


async def office_branches(source: Path) -> list[dict[str, str]]:
    """The office's branches in the owner's repository, newest first."""
    listed = await _git(
        [
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)%09%(objectname)%09%(committerdate:iso-strict)%09%(subject)",
            f"refs/heads/{PREFIX}",
        ],
        source,
    )
    rows = []
    for line in listed.splitlines():
        name, commit, at, subject = (line.split("\t") + ["", "", "", ""])[:4]
        rows.append({"branch": name, "commit": commit, "at": at, "subject": subject})
    return rows


DIFF_CHARS = 200_000


async def branch_diff(source: Path, branch: str, into: str) -> dict[str, object]:
    """What an office/ branch changes against the default branch, for the owner
    to read before merging: the files touched and the patch (capped)."""
    _ours(branch)
    if into.startswith(PREFIX) or not into:
        raise BranchRefused("not_a_default_branch")
    span = f"refs/heads/{into}...refs/heads/{branch}"
    files = [
        line.split("\t", 1)
        for line in (await _git(["diff", "--name-status", span], source)).splitlines()
        if "\t" in line
    ]
    patch = await _git(["diff", "--no-color", span], source)
    return {
        "branch": branch,
        "into": into,
        "files": [{"status": status, "path": path} for status, path in files],
        "patch": patch[:DIFF_CHARS],
        "truncated": len(patch) > DIFF_CHARS,
    }


async def merge_branch(source: Path, branch: str, into: str) -> str:
    """Fast-forward `into` to `branch`: the owner's merge. Returns the new commit."""
    _ours(branch)
    if into.startswith(PREFIX) or not into:
        raise BranchRefused("not_a_default_branch")
    tip = await _git(["rev-parse", f"refs/heads/{branch}"], source)
    base = await _git(["rev-parse", f"refs/heads/{into}"], source)
    try:
        await _git(["merge-base", "--is-ancestor", base, tip], source)
    except BranchRefused:
        raise BranchRefused("not_fast_forward") from None
    try:
        checked_out = await _git(["symbolic-ref", "--short", "-q", "HEAD"], source)
    except BranchRefused:
        checked_out = ""  # a detached checkout
    if checked_out == into:
        if await _git(["status", "--porcelain"], source):
            raise BranchRefused("checkout_has_changes")
        await _git(["merge", "--ff-only", "-q", tip], source)
    else:
        # Moved only if it is still where it was read, so nothing is lost.
        await _git(["update-ref", f"refs/heads/{into}", tip, base], source)
    return tip
