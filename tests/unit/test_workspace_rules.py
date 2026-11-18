"""Which folders inside the approved root a space may be given.

Checked on the server for every path, not only in the folder picker: a path
that names the whole root, leaves it, or reaches a credential or hidden
folder is refused before anything is stored.
"""

import pytest

from app.workspaces import refusal


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("", "whole_root"),
        (".", "whole_root"),
        ("/Users/alex/dev/app", "absolute_path"),
        ("../secrets", "outside_root"),
        ("app/../../etc", "outside_root"),
        ("app/.ssh", "credential_folder"),
        ("Keychains/login", "credential_folder"),
        ("app/.git", "hidden_folder"),
        (".venv", "hidden_folder"),
        ("app\\other", "absolute_path"),
        ("a" * 501, "path_too_long"),
    ],
)
def test_a_folder_that_is_not_safely_inside_the_root_is_refused(path: str, code: str) -> None:
    assert refusal(path) == code


@pytest.mark.parametrize("path", ["quarterdeck_DSH", "clients/acme/site", "notes 2026"])
def test_a_folder_inside_the_root_is_allowed(path: str) -> None:
    assert refusal(path) is None
