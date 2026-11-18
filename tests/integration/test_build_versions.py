"""A build's versions in its project folder: each kept, readable, and restored
as a new one (kept from the engineering gate's tests when Stage 36e removed the
office's own coding agent)."""

from pathlib import Path

import pytest

from tests.conftest import git_is_usable

pytestmark = pytest.mark.integration


def require_git() -> None:
    if not git_is_usable():
        pytest.skip("git is unavailable; run this suite in the container image")


def commit_version(folder: Path, files: dict[str, str], run_id: str, subject: str) -> str:
    import shutil
    import subprocess

    git = shutil.which("git") or "git"
    for name, text in files.items():
        (folder / name).write_text(text)
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([git, "-C", str(folder), "add", "-A"], check=True)  # noqa: S603
    message = ["-m", subject, "-m", f"Office-Run: {run_id}"]
    subprocess.run([git, "-C", str(folder), *identity, "commit", "-q", *message], check=True)  # noqa: S603
    head = subprocess.run(  # noqa: S603
        [git, "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


def test_an_old_version_stays_readable_and_comes_back_as_a_new_one(tmp_path: Path) -> None:
    import io
    import zipfile
    from uuid import uuid4

    from app import builds

    require_git()
    folder = tmp_path / "builds" / "two"
    builds.ensure_repository(folder)
    one = commit_version(folder, {"index.html": "<h1>One</h1>"}, "run-a", "Build: one")
    commit_version(
        folder, {"index.html": "<h1>Two</h1>", "extra.css": "h1{}"}, "run-b", "Build: two"
    )
    versions = builds.versions(folder)
    assert [v["number"] for v in versions] == [1, 2] and versions[1]["latest"]
    assert builds.read_at(folder, one, "index.html") == b"<h1>One</h1>"
    assert [f["path"] for f in builds.files_at(folder, one)] == [".gitignore", "index.html"]
    names = zipfile.ZipFile(io.BytesIO(builds.zipped_at(folder, one, "v1"))).namelist()
    assert "v1/index.html" in names and "v1/extra.css" not in names
    exported = builds.export_at(folder, one, tmp_path / "out")
    assert (exported / "index.html").read_text() == "<h1>One</h1>"
    builds.restore(folder, one, uuid4(), 1)
    after = builds.versions(folder)
    assert len(after) == 3 and after[2]["subject"] == "Restore version 1"
    assert (folder / "index.html").read_text() == "<h1>One</h1>"
    assert not (folder / "extra.css").exists()
    assert builds.read_at(folder, after[1]["commit"], "index.html") == b"<h1>Two</h1>", "v2 kept"


def test_only_the_builds_own_versions_can_be_named(tmp_path: Path) -> None:
    from app import builds

    require_git()
    folder = tmp_path / "builds" / "named"
    builds.ensure_repository(folder)
    commit_version(folder, {"index.html": "x"}, "run-a", "Build: one")
    for bad in ("HEAD", "main", "0000000", "--all", "../x"):
        with pytest.raises(builds.BuildError):
            builds.version_of(folder, bad)


def test_builds_are_told_to_check_their_own_version() -> None:
    """Fix 3: a version 2 build updates check.sh before anything else depends on it."""
    from app.runtimes.engineer import BUILD_RULES

    assert "Update check.sh first" in BUILD_RULES and "earlier version" in BUILD_RULES
