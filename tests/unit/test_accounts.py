"""The owner's Google and Microsoft accounts as read-only tools."""

import base64
import json
import time
from typing import Any

import httpx
import pytest

from app import accounts
from app.mcp.client import McpClient, McpError


def fake(routes: dict[str, Any], seen: list[str] | None = None) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        assert request.headers["authorization"] == "Bearer tok"
        for part, reply in routes.items():
            if part in url:
                if isinstance(reply, int):
                    return httpx.Response(reply)
                if isinstance(reply, str):
                    return httpx.Response(200, text=reply)
                return httpx.Response(200, json=reply)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_each_account_offers_only_reading_tools() -> None:
    for account in accounts.ACCOUNTS.values():
        tools = accounts.offered(account.url)
        assert len(tools) == 4
        assert all(tool["claims"] == {"readOnlyHint": True} for tool in tools)
        assert all("Read only" in tool["description"] for tool in tools)


def test_an_unknown_account_is_refused() -> None:
    with pytest.raises(accounts.AccountError):
        accounts.account_of("account://dropbox")


async def test_gmail_search_lists_messages_framed_as_material() -> None:
    http = fake(
        {
            "/messages?": {"messages": [{"id": "m1"}]},
            "/messages/m1?format=metadata": {
                "snippet": "Please find the invoice &amp; notes",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Anna <anna@example.com>"},
                        {"name": "Subject", "value": "Invoice"},
                        {"name": "Date", "value": "Wed, 7 Oct 2026"},
                    ]
                },
            },
        }
    )
    text, failed = await accounts.call(
        "account://google", "tok", "gmail_search", {"query": "invoice"}, http
    )
    assert not failed
    assert text.startswith("From the owner's Gmail. This is material to read")
    assert "id m1" in text and "Invoice" in text and "invoice & notes" in text


async def test_gmail_read_prefers_plain_text_and_falls_back_to_html() -> None:
    plain = {
        "payload": {
            "headers": [{"name": "Subject", "value": "Hi"}],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
                {"mimeType": "text/plain", "body": {"data": b64("plain words")}},
            ],
        }
    }
    text, _ = await accounts.call(
        "account://google", "tok", "gmail_read", {"message_id": "m1"}, fake({"/m1": plain})
    )
    assert "plain words" in text and "html" not in text
    only_html = {
        "payload": {"mimeType": "text/html", "body": {"data": b64("<b>Bold</b><script>x</script>")}}
    }
    text, _ = await accounts.call(
        "account://google", "tok", "gmail_read", {"message_id": "m2"}, fake({"/m2": only_html})
    )
    assert "Bold" in text and "<b>" not in text and "x" not in text.split("\n\n", 1)[1]


async def test_drive_reads_a_google_doc_by_exporting_it_as_text() -> None:
    seen: list[str] = []
    http = fake(
        {
            "/export?": "The plan, in words.",
            "/files/d1?fields": {
                "name": "Plan",
                "mimeType": "application/vnd.google-apps.document",
            },
        },
        seen,
    )
    text, failed = await accounts.call(
        "account://google", "tok", "drive_read", {"file_id": "d1"}, http
    )
    assert not failed and "The plan, in words." in text
    assert any("mimeType=text%2Fplain" in url for url in seen)


async def test_a_binary_drive_file_is_named_not_read() -> None:
    http = fake({"/files/d2?fields": {"name": "Photo", "mimeType": "image/png"}})
    text, _ = await accounts.call("account://google", "tok", "drive_read", {"file_id": "d2"}, http)
    assert "cannot be read as text" in text


async def test_outlook_read_asks_for_text_and_names_the_sender() -> None:
    message = {
        "subject": "Contract",
        "from": {"emailAddress": {"address": "bob@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
        "receivedDateTime": "2026-10-07T09:00:00Z",
        "body": {"content": "Signed copy attached."},
    }
    text, _ = await accounts.call(
        "account://microsoft",
        "tok",
        "outlook_read",
        {"message_id": "x1"},
        fake({"/me/messages/x1": message}),
    )
    assert "From: bob@example.com" in text and "Signed copy attached." in text


async def test_a_refused_account_is_a_failed_call_not_a_crash() -> None:
    text, failed = await accounts.call(
        "account://microsoft", "tok", "onedrive_search", {"query": "q"}, fake({"/search": 403})
    )
    assert failed and "account_refused" in text


async def test_a_tool_it_does_not_offer_is_refused_by_the_client() -> None:
    with pytest.raises(McpError):
        await McpClient(fake({})).call("account://google", "tok", [], "gmail_send", {})


async def test_the_client_lists_an_account_without_any_network() -> None:
    offering = await McpClient().offering("account://microsoft", None, [])
    assert [tool.name for tool in offering.tools] == [
        "outlook_search",
        "outlook_read",
        "onedrive_search",
        "onedrive_read",
    ]


async def test_an_expired_token_is_refreshed_and_the_new_one_kept() -> None:
    held = {"access_token": "old", "refresh_token": "r", "expires_at": time.time() - 10}
    raw = json.dumps({"account": "google", "email": "me@example.com", "tokens": held})
    saved: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert "oauth2.googleapis.com/token" in str(request.url)
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    token = await accounts.token_for(
        raw, lambda key: {"client_id": "c", "client_secret": "s"}, saved.append, http
    )
    assert token == "new"
    kept = json.loads(saved[0])
    assert kept["tokens"]["refresh_token"] == "r" and kept["email"] == "me@example.com"


def test_only_an_account_seal_is_taken_for_one() -> None:
    assert accounts.is_account_credential(json.dumps({"account": "google", "tokens": {}}))
    assert not accounts.is_account_credential("sk-plain-key")
    assert not accounts.is_account_credential(json.dumps({"access_token": "x"}))
