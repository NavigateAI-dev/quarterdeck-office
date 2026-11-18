from pathlib import Path

import pytest

from app.runtimes.workspace import ScopeViolation, path_allowed, within


@pytest.mark.parametrize(
    "path,permitted,expected",
    [
        ("app/export.py", ["app/export.py"], True),
        ("app/export.py", ["app/*.py"], True),
        ("app/nested/deep.py", ["app/**"], True),
        ("app/secret.py", ["app/export.py"], False),
        ("README.md", ["app/*.py"], False),
        (".github/workflows/deploy.yml", ["app/*.py"], False),
        ("app/export.py", [], False),
    ],
)
def test_only_approved_paths_are_allowed(path: str, permitted: list[str], expected: bool) -> None:
    assert path_allowed(path, permitted) is expected


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "app/../../etc/passwd", "/etc/passwd", "../../.ssh/id_rsa"],
)
def test_traversal_is_never_allowed(path: str) -> None:
    """A pattern must not be able to authorize something outside the repository."""
    assert path_allowed(path, ["**", "*", "../outside.py", "/etc/passwd"]) is False


def test_an_approval_naming_no_paths_authorizes_nothing() -> None:
    assert path_allowed("app/anything.py", []) is False


def test_containment_follows_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "target.txt").write_text("secret")
    link = root / "escape"
    link.symlink_to(outside)
    assert within(root, root / "inside.txt")
    assert not within(root, link / "target.txt")
    assert not within(root, outside / "target.txt")


def test_scope_violation_names_the_offending_paths() -> None:
    violation = ScopeViolation(["app/secret.py", "deploy.sh"])
    assert "app/secret.py" in str(violation)
