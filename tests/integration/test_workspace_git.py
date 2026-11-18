"""Workspace isolation against real git. Requires git, which lives in the image."""

from pathlib import Path
from uuid import uuid4

import pytest

from app.runtimes.base import ExecutionLimits, run_bounded
from app.runtimes.workspace import (
    ScopeViolation,
    assert_within_scope,
    base_commit_unchanged,
    changed_paths,
    create_workspace,
    diff,
    discard,
)
from tests.conftest import git_is_usable

pytestmark = pytest.mark.integration

GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def require_git() -> None:
    if not git_is_usable():
        pytest.skip("git is unavailable; run this suite in the container image")


async def make_repository(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "app").mkdir()
    (path / "app" / "export.py").write_text("VALUE = 1\n")
    (path / "README.md").write_text("fixture\n")
    # As in a real Python repository; the test command writes bytecode.
    (path / ".gitignore").write_text("__pycache__/\n")
    limits = ExecutionLimits(environment=GIT_ENVIRONMENT)
    await run_bounded(["git", "init", "--quiet", "--initial-branch=main", "."], path, limits)
    await run_bounded(["git", "add", "."], path, limits)
    await run_bounded(["git", "commit", "--quiet", "-m", "fixture"], path, limits)
    return path


@pytest.fixture
async def workspace(tmp_path: Path):  # type: ignore[no-untyped-def]
    require_git()
    source = await make_repository(tmp_path / "source")
    created = await create_workspace(source, tmp_path / "work", uuid4())
    try:
        yield created
    finally:
        discard(created)


async def test_a_clone_is_isolated_from_its_source(workspace, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    assert (tmp_path / "source" / "app" / "export.py").read_text() == "VALUE = 1\n"
    assert len(workspace.base_commit) == 40


async def test_changes_are_detected_including_new_files(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    (workspace.repository / "app" / "added.py").write_text("NEW = True\n")
    assert await changed_paths(workspace) == ["app/added.py", "app/export.py"]


async def test_a_change_inside_the_approved_paths_is_accepted(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    assert await assert_within_scope(workspace, ["app/*.py"]) == ["app/export.py"]


async def test_a_change_outside_the_approved_paths_is_refused(workspace) -> None:  # type: ignore[no-untyped-def]
    """The check reads the real diff, not the runtime's account of its work."""
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    (workspace.repository / "README.md").write_text("tampered\n")
    with pytest.raises(ScopeViolation) as caught:
        await assert_within_scope(workspace, ["app/*.py"])
    assert caught.value.paths == ["README.md"]


async def test_an_unapproved_new_file_is_refused(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.repository / "deploy.sh").write_text("#!/bin/sh\n")
    with pytest.raises(ScopeViolation):
        await assert_within_scope(workspace, ["app/*.py"])


async def test_the_diff_shows_the_actual_change(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    body = await diff(workspace)
    assert "-VALUE = 1" in body
    assert "+VALUE = 2" in body


async def test_an_unchanged_workspace_reports_no_changes(workspace) -> None:  # type: ignore[no-untyped-def]
    assert await changed_paths(workspace) == []
    assert await assert_within_scope(workspace, ["app/*.py"]) == []


async def test_a_moved_base_commit_is_detected(workspace) -> None:  # type: ignore[no-untyped-def]
    assert await base_commit_unchanged(workspace, workspace.base_commit)
    limits = ExecutionLimits(environment=GIT_ENVIRONMENT)
    (workspace.repository / "app" / "export.py").write_text("VALUE = 3\n")
    await run_bounded(["git", "add", "."], workspace.repository, limits)
    await run_bounded(["git", "commit", "--quiet", "-m", "moved"], workspace.repository, limits)
    assert not await base_commit_unchanged(workspace, workspace.base_commit)


async def test_discard_removes_the_attempt_directory(workspace) -> None:  # type: ignore[no-untyped-def]
    path = workspace.repository
    discard(workspace)
    assert not path.exists()


async def test_each_attempt_works_on_its_own_branch(workspace) -> None:  # type: ignore[no-untyped-def]
    """SPEC 8.5 requires an isolated branch, so the base branch is never moved."""
    from app.runtimes.workspace import BRANCH_PREFIX, current_branch

    assert workspace.branch.startswith(BRANCH_PREFIX)
    assert await current_branch(workspace) == workspace.branch


async def test_the_source_branch_is_untouched_by_an_attempt(  # type: ignore[no-untyped-def]
    workspace, tmp_path: Path
) -> None:
    limits = ExecutionLimits(environment=GIT_ENVIRONMENT)
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    await run_bounded(["git", "add", "."], workspace.repository, limits)
    await run_bounded(["git", "commit", "--quiet", "-m", "work"], workspace.repository, limits)
    source_head = await run_bounded(["git", "rev-parse", "HEAD"], tmp_path / "source", limits)
    assert source_head.stdout.strip() == workspace.base_commit


async def commit_all(workspace, message: str) -> None:  # type: ignore[no-untyped-def]
    limits = ExecutionLimits(environment=GIT_ENVIRONMENT)
    await run_bounded(["git", "add", "--all"], workspace.repository, limits)
    await run_bounded(["git", "commit", "--quiet", "-m", message], workspace.repository, limits)


async def test_a_committed_forbidden_change_is_still_refused(workspace) -> None:  # type: ignore[no-untyped-def]
    """Committing a change must not hide it from the base-relative scope check."""
    (workspace.repository / "README.md").write_text("tampered\n")
    await commit_all(workspace, "hide it")
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    with pytest.raises(ScopeViolation) as caught:
        await assert_within_scope(workspace, ["app/*.py"])
    assert caught.value.paths == ["README.md"]


async def test_a_rename_checks_both_the_old_and_new_path(workspace) -> None:  # type: ignore[no-untyped-def]
    """Moving a protected file into scope removes it from where it was."""
    (workspace.repository / "README.md").rename(workspace.repository / "app" / "README.md")
    await commit_all(workspace, "move")
    with pytest.raises(ScopeViolation) as caught:
        await assert_within_scope(workspace, ["app/*"])
    assert caught.value.paths == ["README.md"]


async def test_staged_and_committed_changes_are_all_listed(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.repository / "app" / "export.py").write_text("VALUE = 2\n")
    await commit_all(workspace, "one")
    (workspace.repository / "app" / "staged.py").write_text("S = 1\n")
    await run_bounded(["git", "add", "app/staged.py"], workspace.repository)
    (workspace.repository / "app" / "untracked.py").write_text("U = 1\n")
    assert await changed_paths(workspace) == [
        "app/export.py",
        "app/staged.py",
        "app/untracked.py",
    ]


async def test_an_attempt_starts_at_the_approved_commit_of_another_branch(
    tmp_path: Path,
) -> None:
    """The source's checked-out branch is irrelevant; the approved commit is used."""
    from app.orchestrator.gate import resolve_base_commit

    require_git()
    source = await make_repository(tmp_path / "source")
    limits = ExecutionLimits(environment=GIT_ENVIRONMENT)
    await run_bounded(["git", "checkout", "--quiet", "-b", "release"], source, limits)
    (source / "app" / "export.py").write_text("VALUE = 7\n")
    await run_bounded(["git", "commit", "--quiet", "-am", "release"], source, limits)
    await run_bounded(["git", "checkout", "--quiet", "main"], source, limits)

    release = resolve_base_commit(source, "release")
    main = resolve_base_commit(source, "main")
    assert release and main and release != main
    assert resolve_base_commit(source, "no-such-branch") is None

    created = await create_workspace(source, tmp_path / "work", uuid4(), release)
    try:
        assert created.base_commit == release
        assert (created.repository / "app" / "export.py").read_text() == "VALUE = 7\n"
    finally:
        discard(created)
