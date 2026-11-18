from pathlib import Path

import pytest

from scripts.pick_folder import refusal, set_env_value


@pytest.mark.parametrize(
    "path,reason",
    [
        ("/", "whole_disk"),
        ("HOME", "entire_home_folder"),
        ("HOME/.ssh", "credential_folder"),
        ("HOME/Library/Keychains", "credential_folder"),
        ("HOME/Documents/Research", None),
    ],
)
def test_broad_or_sensitive_folders_are_refused(path: str, reason: str | None) -> None:
    home = Path("/Users/owner")
    assert refusal(Path(path.replace("HOME", str(home))), home) == reason


def test_the_env_value_is_replaced_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nLOCAL_SOURCES_DIR=./sources\nB=2\n")
    set_env_value(env, "LOCAL_SOURCES_DIR", "/Users/owner/Documents/Research")
    assert env.read_text() == "A=1\nLOCAL_SOURCES_DIR=/Users/owner/Documents/Research\nB=2\n"
    assert env.stat().st_mode & 0o077 == 0


def test_a_missing_env_value_is_appended(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    set_env_value(env, "LOCAL_SOURCES_DIR", "/data/docs")
    assert env.read_text() == "A=1\nLOCAL_SOURCES_DIR=/data/docs\n"
