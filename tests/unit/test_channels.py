"""The channel abstraction and the Slack adapter, against mocked Slack HTTP."""

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.channels.base import ChannelError, OutboundMessage
from app.channels.registry import adapter_for, available_kinds
from app.channels.slack import SlackAdapter

TOKEN = "xoxb-test-bot-token-000000"


def slack(handler: Any) -> SlackAdapter:
    return SlackAdapter(transport=httpx.MockTransport(handler))


def test_the_authorize_url_carries_scopes_state_and_https_redirect() -> None:
    url = SlackAdapter().authorize_url(
        client_id="123.456",
        redirect_uri="https://office.example.com/channels/slack/oauth/callback",
        state="s" * 43,
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "slack.com" and parsed.path == "/oauth/v2/authorize"
    assert query["client_id"] == ["123.456"]
    assert query["state"] == ["s" * 43]
    assert "chat:write" in query["scope"][0].split(",")


def test_the_oauth_install_asks_for_the_same_scopes_as_the_manifest() -> None:
    """R21: one list, so a bot installed by OAuth can speak as an agent too."""
    from app.channels.manifest import SCOPES

    url = SlackAdapter().authorize_url(
        client_id="1", redirect_uri="https://office.example.com/callback", state="s" * 43
    )
    asked = parse_qs(urlparse(url).query)["scope"][0].split(",")
    assert asked == list(SCOPES) and "chat:write.customize" in asked


def test_a_non_https_redirect_is_refused() -> None:
    with pytest.raises(ChannelError, match="redirect_must_be_https"):
        SlackAdapter().authorize_url(
            client_id="1", redirect_uri="http://127.0.0.1:8000/callback", state="x" * 43
        )


async def test_the_code_exchange_returns_an_installation_without_echoing_secrets() -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": TOKEN,
                "token_type": "bot",
                "scope": "chat:write,channels:read",
                "bot_user_id": "U0BOT",
                "app_id": "A0APP",
                "team": {"id": "T0TEAM", "name": "Northwind"},
                "authed_user": {"id": "U0OWNER"},
            },
        )

    installation = await slack(handle).exchange_code(
        code="c0de", client_id="1", client_secret="shh", redirect_uri="https://x.example/cb"
    )
    assert seen[0]["client_secret"] == "shh"
    assert installation.workspace_id == "T0TEAM"
    assert installation.workspace_name == "Northwind"
    assert installation.bot_token == TOKEN
    assert installation.owner_external_id == "U0OWNER"
    assert TOKEN not in repr(installation)


async def test_a_slack_error_becomes_a_code() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

    with pytest.raises(ChannelError, match="invalid_code"):
        await slack(handle).exchange_code(
            code="x", client_id="1", client_secret="s", redirect_uri=""
        )


async def test_a_bot_token_is_verified_before_it_is_trusted() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            200, json={"ok": True, "team": "Northwind", "team_id": "T0TEAM", "user_id": "U0BOT"}
        )

    installation = await slack(handle).verify_token(TOKEN)
    assert (installation.workspace_id, installation.bot_user_id) == ("T0TEAM", "U0BOT")


async def test_channels_the_bot_can_post_to_are_listed() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "office-updates", "is_member": True, "is_private": False},
                    {"id": "C2", "name": "random", "is_member": False, "is_private": False},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )

    channels = await slack(handle).list_destinations(TOKEN)
    assert [(d.external_id, d.name, d.can_post) for d in channels] == [
        ("C1", "#office-updates", True),
        ("C2", "#random", False),
    ]


async def test_a_message_is_posted_and_its_reference_returned() -> None:
    posted: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "ts": "1726740000.000100", "channel": "C1"})

    result = await slack(handle).send(TOKEN, "C1", OutboundMessage(text="Run needs your approval"))
    assert result.external_ref == "C1:1726740000.000100"
    assert posted[0]["channel"] == "C1"
    assert posted[0]["unfurl_links"] is False


async def test_a_timeout_after_sending_is_an_unknown_outcome() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ChannelError, match="outcome_unknown"):
        await slack(handle).send(TOKEN, "C1", OutboundMessage(text="x"))


async def test_a_failure_before_sending_is_a_plain_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(ChannelError, match="unreachable"):
        await slack(handle).send(TOKEN, "C1", OutboundMessage(text="x"))


def test_the_registry_offers_slack_and_marks_others_planned() -> None:
    kinds = {kind["kind"]: kind for kind in available_kinds()}
    assert kinds["slack"]["status"] == "available"
    assert kinds["teams"]["status"] == "planned"
    assert kinds["discord"]["status"] == "planned"
    assert isinstance(adapter_for("slack"), SlackAdapter)
    with pytest.raises(ChannelError, match="channel_not_available"):
        adapter_for("teams")


async def test_buttons_go_with_the_message() -> None:
    posted: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "ts": "1.1", "channel": "C1"})

    blocks = [{"type": "actions", "elements": []}]
    await slack(handle).send(TOKEN, "C1", OutboundMessage(text="x", thread="t1", blocks=blocks))
    assert posted[0]["blocks"] == blocks and posted[0]["thread_ts"] == "t1"


async def test_a_message_is_edited_in_place_without_its_buttons() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    await slack(handle).update(TOKEN, "C1", "1.1", OutboundMessage(text="Answered"))
    path, body = calls[0]
    assert path == "/api/chat.update"
    assert body["channel"] == "C1" and body["ts"] == "1.1" and body["text"] == "Answered"
    assert body["blocks"] == [{"type": "section", "text": {"type": "mrkdwn", "text": "Answered"}}]


async def test_a_file_is_uploaded_into_the_thread() -> None:
    seen: list[tuple[str, bytes]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.content))
        if request.url.path == "/api/files.getUploadURLExternal":
            return httpx.Response(
                200, json={"ok": True, "upload_url": "https://files.slack.com/u/1", "file_id": "F1"}
            )
        if request.url.host == "files.slack.com":
            return httpx.Response(200, text="OK - 5")
        return httpx.Response(200, json={"ok": True, "files": [{"id": "F1"}]})

    await slack(handle).upload(TOKEN, "C1", "t1", "brief-v1.docx", b"hello")
    asked, uploaded, completed = seen
    assert parse_qs(asked[1].decode()) == {"filename": ["brief-v1.docx"], "length": ["5"]}
    assert uploaded == ("https://files.slack.com/u/1", b"hello")
    assert json.loads(completed[1]) == {
        "files": [{"id": "F1", "title": "brief-v1.docx"}],
        "channel_id": "C1",
        "thread_ts": "t1",
    }


async def test_a_missing_file_permission_is_a_code() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "missing_scope"})

    with pytest.raises(ChannelError, match="missing_scope"):
        await slack(handle).upload(TOKEN, "C1", None, "a.docx", b"x")
