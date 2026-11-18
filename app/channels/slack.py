"""Slack adapter: OAuth v2 install, token verification, channels, messages.

OAuth follows docs.slack.dev/authentication/installing-with-oauth: an
authorize URL with scopes and a single-use `state`, then `oauth.v2.access`
with the client secret. Slack requires HTTPS redirect URLs, so the OAuth path
needs an HTTPS address that reaches this office; a local office can instead
verify a bot token from the app's "Install to Workspace" page. Inbound
requests will use Socket Mode, which needs no public URL.
"""

from typing import Any
from urllib.parse import urlencode

import httpx

from app.channels.base import (
    ChannelError,
    DeliveryResult,
    Destination,
    Installation,
    OutboundMessage,
)
from app.channels.manifest import SCOPES
from app.channels.slack_format import sections

API = "https://slack.com/api"
AUTHORIZE = "https://slack.com/oauth/v2/authorize"
# The OAuth install asks for what the manifest asks for: one list.
BOT_SCOPES = SCOPES


class SlackAdapter:
    kind = "slack"

    def __init__(
        self, timeout_seconds: float = 15.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.timeout = timeout_seconds
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport, trust_env=False)

    @staticmethod
    def _checked(response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 429:
            raise ChannelError("rate_limited")
        if response.status_code >= 500:
            raise ChannelError("slack_unavailable")
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            raise ChannelError("malformed_response") from None
        if not body.get("ok"):
            raise ChannelError(str(body.get("error") or "slack_error")[:64])
        return body

    def authorize_url(self, client_id: str, redirect_uri: str, state: str) -> str:
        if not redirect_uri.startswith("https://"):
            raise ChannelError("redirect_must_be_https")
        query = {
            "client_id": client_id,
            "scope": ",".join(BOT_SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE}?{urlencode(query)}"

    async def exchange_code(
        self, code: str, client_id: str, client_secret: str, redirect_uri: str
    ) -> Installation:
        form = {"code": code, "client_id": client_id, "client_secret": client_secret}
        if redirect_uri:
            form["redirect_uri"] = redirect_uri
        try:
            async with self._client() as client:
                response = await client.post(f"{API}/oauth.v2.access", data=form)
        except httpx.HTTPError:
            raise ChannelError("unreachable") from None
        body = self._checked(response)
        if body.get("token_type") != "bot" or not body.get("access_token"):
            raise ChannelError("no_bot_token")
        team = body.get("team") or {}
        return Installation(
            workspace_id=str(team.get("id", "")),
            workspace_name=str(team.get("name", "")),
            bot_user_id=str(body.get("bot_user_id", "")),
            bot_token=str(body["access_token"]),
            scopes=tuple(str(body.get("scope", "")).split(",")),
            owner_external_id=(body.get("authed_user") or {}).get("id"),
        )

    async def verify_token(self, token: str) -> Installation:
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{API}/auth.test", headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError:
            raise ChannelError("unreachable") from None
        body = self._checked(response)
        return Installation(
            workspace_id=str(body.get("team_id", "")),
            workspace_name=str(body.get("team", "")),
            bot_user_id=str(body.get("user_id", "")),
            bot_token=token,
            scopes=tuple(response.headers.get("x-oauth-scopes", "").split(",")),
        )

    async def list_destinations(self, token: str) -> list[Destination]:
        destinations: list[Destination] = []
        cursor = ""
        try:
            async with self._client() as client:
                for _ in range(10):  # bounded pagination
                    response = await client.get(
                        f"{API}/conversations.list",
                        params={
                            "types": "public_channel,private_channel",
                            "exclude_archived": "true",
                            "limit": "200",
                            **({"cursor": cursor} if cursor else {}),
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    body = self._checked(response)
                    for channel in body.get("channels", []):
                        destinations.append(
                            Destination(
                                external_id=str(channel["id"]),
                                name=f"#{channel.get('name', '')}",
                                can_post=bool(channel.get("is_member")),
                            )
                        )
                    cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
                    if not cursor:
                        break
        except httpx.HTTPError:
            raise ChannelError("unreachable") from None
        return destinations

    async def find_message(
        self, token: str, destination: str, text: str, since: float
    ) -> str | None:
        """Ask Slack whether this exact message is in the channel.

        Needs history access for that conversation. Without the scope Slack
        refuses, and the office asks the owner instead of guessing.
        """
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{API}/conversations.history",
                    params={"channel": destination, "oldest": f"{since:.6f}", "limit": 50},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ChannelError("unreachable") from None
        except httpx.HTTPError:
            raise ChannelError("outcome_unknown") from None
        body = self._checked(response)
        for message in body.get("messages") or []:
            if str(message.get("text", "")).strip() == text.strip():
                return f"{destination}:{message.get('ts', '')}"
        return None

    async def message_text(self, token: str, channel: str, ts: str) -> str:
        """The words of one message, for a 💡 the owner put on it.

        Read from the conversation's history, or from its thread when the
        message is a reply. Needs history access to that conversation
        (im:history, channels:history, groups:history).
        """
        for method in ("conversations.history", "conversations.replies"):
            params = {"channel": channel, "latest": ts, "oldest": ts, "inclusive": "true"}
            if method == "conversations.replies":
                params["ts"] = ts
            try:
                async with self._client() as client:
                    response = await client.get(
                        f"{API}/{method}",
                        params={**params, "limit": 1},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError:
                raise ChannelError("unreachable") from None
            body = self._checked(response)
            for message in body.get("messages") or []:
                if str(message.get("ts")) == ts and str(message.get("text") or "").strip():
                    return str(message["text"]).strip()
        raise ChannelError("message_not_found")

    async def whisper(self, token: str, channel: str, user: str, text: str) -> None:
        """A message only that person sees in the channel (an ephemeral message)."""
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{API}/chat.postEphemeral",
                    json={"channel": channel, "user": user, "text": text},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            raise ChannelError("unreachable") from None
        self._checked(response)

    async def send(self, token: str, destination: str, message: OutboundMessage) -> DeliveryResult:
        payload = {
            "channel": destination,
            "text": message.text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if message.thread:
            payload["thread_ts"] = message.thread
        if message.blocks:
            payload["blocks"] = message.blocks
        if message.username:
            # Needs chat:write.customize; without it Slack ignores these and
            # the message still arrives under the app's own name.
            payload["username"] = message.username[:80]
            payload["icon_emoji"] = message.icon_emoji or ":speech_balloon:"
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{API}/chat.postMessage",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ChannelError("unreachable") from None  # Never left this machine.
        except httpx.HTTPError:
            # The request may have reached Slack: never retry blindly.
            raise ChannelError("outcome_unknown") from None
        body = self._checked(response)
        return DeliveryResult(
            external_ref=f"{body.get('channel', destination)}:{body.get('ts', '')}"
        )

    async def update(self, token: str, destination: str, ts: str, message: OutboundMessage) -> None:
        """Replace a message the office posted: progress as it moves, answered buttons."""
        payload: dict[str, Any] = {"channel": destination, "ts": ts, "text": message.text}
        # Without buttons it is the text as plain blocks: Slack keeps a
        # message's old blocks unless it is given new ones.
        payload["blocks"] = message.blocks or sections(message.text)
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{API}/chat.update",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            # Editing twice shows the same message, so an unknown outcome is
            # simply tried again later.
            raise ChannelError("unreachable") from None
        self._checked(response)

    async def upload(
        self, token: str, destination: str, thread: str | None, name: str, data: bytes
    ) -> None:
        """Share a file in a conversation (files.getUploadURLExternal, then complete)."""
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with self._client() as client:
                asked = self._checked(
                    await client.post(
                        f"{API}/files.getUploadURLExternal",
                        data={"filename": name, "length": str(len(data))},
                        headers=headers,
                    )
                )
                url, file_id = str(asked.get("upload_url") or ""), str(asked.get("file_id") or "")
                if not url.startswith("https://") or not file_id:
                    raise ChannelError("malformed_response")
                sent = await client.post(url, content=data)
                if sent.status_code >= 400:
                    raise ChannelError("upload_refused")
                body: dict[str, Any] = {
                    "files": [{"id": file_id, "title": name}],
                    "channel_id": destination,
                }
                if thread:
                    body["thread_ts"] = thread
                self._checked(
                    await client.post(
                        f"{API}/files.completeUploadExternal", json=body, headers=headers
                    )
                )
        except httpx.HTTPError:
            raise ChannelError("unreachable") from None

    async def download(self, token: str, url: str, limit: int) -> bytes:
        """A file shared in Slack. Only Slack's own file host is asked, with the
        bot token, and never more than `limit` bytes are kept."""
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ("files.slack.com",):
            raise ChannelError("file_address_not_slack")
        try:
            async with self._client() as client:
                async with client.stream(
                    "GET", url, headers={"Authorization": f"Bearer {token}"}
                ) as response:
                    if response.status_code in (401, 403):
                        raise ChannelError("missing_scope")
                    if response.status_code >= 400:
                        raise ChannelError("file_unavailable")
                    if "text/html" in response.headers.get("content-type", ""):
                        # Slack answers a token without files:read with its sign-in page.
                        raise ChannelError("missing_scope")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ChannelError("file_too_large")
        except httpx.HTTPError:
            raise ChannelError("unreachable") from None
        return bytes(data)
