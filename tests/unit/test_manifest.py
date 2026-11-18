import json
from urllib.parse import unquote

from app.channels.manifest import CREATE_URL, SCOPES, create_app_url, slack_manifest


def test_the_manifest_names_the_agent_and_asks_only_for_the_allowed_scopes() -> None:
    manifest = slack_manifest("Researcher – pricing <script>", "researcher")
    assert manifest["display_information"]["name"] == "Researcher  pricing script"
    assert manifest["oauth_config"]["scopes"]["bot"] == list(SCOPES)
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert manifest["features"]["app_home"]["messages_tab_enabled"] is True


def test_long_names_fit_slacks_limit() -> None:
    assert len(slack_manifest("x" * 80, "critic")["display_information"]["name"]) == 35


def test_the_create_url_carries_the_manifest() -> None:
    manifest = slack_manifest("Critic", "critic")
    url = create_app_url(manifest)
    assert url.startswith(CREATE_URL)
    assert json.loads(unquote(url.removeprefix(CREATE_URL))) == manifest
