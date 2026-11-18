"""Work access: the office writes office/ branches, the owner merges.

Against real repositories in a temporary folder: the owner's checkout and its
default branch are never written by the office's work; a merge is the owner's,
fast-forward only.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.workspace_git import BranchRefused, branch_for, land_on_branch, merge_branch

GIT = shutil.which("git") or "git"


def git(where: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed binary, argument vector, in a test
        [GIT, "-c", "user.name=T", "-c", "user.email=t@x", *args],
        cwd=where,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def repos(tmp_path: Path) -> tuple[Path, Path]:
    """The owner's repository on main, and the office's clone with work on its branch."""
    owner = tmp_path / "site"
    owner.mkdir()
    git(owner, "init", "-q", "-b", "main")
    (owner / "index.html").write_text("<h1>Acme</h1>\n")
    git(owner, "add", "-A")
    git(owner, "commit", "-q", "-m", "Start")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(owner), str(clone))
    git(clone, "checkout", "-q", "-b", "quarterdeck/attempt-1")
    (clone / "pricing.html").write_text("<p>Pilot: 450</p>\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "Pricing page")
    return owner, clone


def test_a_branch_is_named_after_its_thread() -> None:
    assert (
        branch_for("Pricing for the pilot!", "8f3a9c21-x") == "office/pricing-for-the-pilot-8f3a9c"
    )
    assert branch_for("", "ab") == "office/work-ab"


async def test_finished_work_lands_as_an_office_branch_and_nothing_else_moves(
    tmp_path: Path,
) -> None:
    owner, clone = repos(tmp_path)
    main_before = git(owner, "rev-parse", "main")

    tip = await land_on_branch(clone, "quarterdeck/attempt-1", owner, "office/pricing-8f3a9c")

    assert git(owner, "rev-parse", "office/pricing-8f3a9c") == tip
    assert git(owner, "rev-parse", "main") == main_before, "the default branch is not written"
    assert not (owner / "pricing.html").exists(), "the owner's checkout is not touched"
    assert git(owner, "status", "--porcelain") == ""


async def test_the_office_writes_only_its_own_branches(tmp_path: Path) -> None:
    owner, clone = repos(tmp_path)
    with pytest.raises(BranchRefused) as refused:
        await land_on_branch(clone, "quarterdeck/attempt-1", owner, "main")
    assert refused.value.code == "not_an_office_branch"


async def test_the_owners_merge_fast_forwards_a_clean_checkout(tmp_path: Path) -> None:
    owner, clone = repos(tmp_path)
    tip = await land_on_branch(clone, "quarterdeck/attempt-1", owner, "office/pricing-1")

    merged = await merge_branch(owner, "office/pricing-1", "main")

    assert merged == tip == git(owner, "rev-parse", "main")
    assert (owner / "pricing.html").exists()


async def test_a_checkout_with_changes_is_not_merged_into(tmp_path: Path) -> None:
    owner, clone = repos(tmp_path)
    await land_on_branch(clone, "quarterdeck/attempt-1", owner, "office/pricing-1")
    (owner / "index.html").write_text("<h1>Acme, edited by the owner</h1>\n")

    with pytest.raises(BranchRefused) as refused:
        await merge_branch(owner, "office/pricing-1", "main")

    assert refused.value.code == "checkout_has_changes"
    assert "edited by the owner" in (owner / "index.html").read_text()


async def test_a_default_branch_that_moved_on_is_never_overwritten(tmp_path: Path) -> None:
    owner, clone = repos(tmp_path)
    await land_on_branch(clone, "quarterdeck/attempt-1", owner, "office/pricing-1")
    (owner / "about.html").write_text("<p>About</p>\n")
    git(owner, "add", "-A")
    git(owner, "commit", "-q", "-m", "The owner's own work")

    with pytest.raises(BranchRefused) as refused:
        await merge_branch(owner, "office/pricing-1", "main")

    assert refused.value.code == "not_fast_forward"


async def test_a_default_branch_not_checked_out_moves_by_its_ref_only(tmp_path: Path) -> None:
    owner, clone = repos(tmp_path)
    tip = await land_on_branch(clone, "quarterdeck/attempt-1", owner, "office/pricing-1")
    git(owner, "checkout", "-q", "-b", "owner-feature")

    await merge_branch(owner, "office/pricing-1", "main")

    assert git(owner, "rev-parse", "main") == tip
    assert git(owner, "symbolic-ref", "--short", "HEAD") == "owner-feature"
