"""Re-checking the catalog for a person to review."""

from scripts.mcp_catalog import drift, sign_in_seen


def test_an_answer_without_a_challenge_needs_no_sign_in() -> None:
    assert sign_in_seen(200, "") == "none"


def test_a_401_is_a_sign_in_of_some_kind() -> None:
    assert sign_in_seen(401, 'Bearer resource_metadata="https://x/.well-known/oauth"') == "sign-in"
    assert sign_in_seen(401, "") == "sign-in"


def test_anything_else_is_not_answering() -> None:
    assert sign_in_seen(404, "") == "not answering"
    assert sign_in_seen(None, "") == "not answering"


def test_an_entry_that_still_matches_has_no_drift() -> None:
    assert drift({"sign_in": "none"}, "none") is None
    assert drift({"sign_in": "key"}, "sign-in") is None
    assert drift({"sign_in": "account"}, "sign-in") is None


def test_a_server_that_started_asking_for_sign_in_is_drift() -> None:
    assert "now asks for sign-in" in (drift({"sign_in": "none"}, "sign-in") or "")


def test_a_server_that_stopped_answering_is_drift() -> None:
    assert "not answering" in (drift({"sign_in": "key"}, "not answering") or "")
