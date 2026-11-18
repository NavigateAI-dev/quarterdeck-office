"""Reaching an MCP server, and asking what it offers.

The office can tell an agent how to think but cannot give it a new capability:
skills are instructions only, and tools are compiled in. MCP is how a tool the
office does not hard-code — a vendor API, a repository, a database — becomes
available without the office learning it in Python.

Three rules from the specification decide the shape of this file.

**Streamable HTTP only.** The other standard transport, stdio, launches the
server as a subprocess with the owner's permissions. That is the same question
as running third-party extensions in-process, answered the same way: no.

**Annotations are untrusted.** The specification says a client MUST treat tool
annotations as untrusted unless the server is trusted. So `readOnlyHint` is
carried and shown, and never believed: it cannot decide whether a call needs the
owner's approval. Only the owner decides that.

**The address is an SSRF boundary.** A server URL is checked with the same rule
as a page fetched from model output — public HTTPS, resolved rather than
trusted by name — and against the office's host allow-list.

Calling a tool is here too, and is deliberately thin: it sends the request and
returns what came back. Everything that decides *whether* a call may happen —
the owner's stance on the tool, the office's policies, the run's budget — lives
outside it, in `app/mcp/calling.py`. A transport should not be able to grant
anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.tools.fetch import FetchError, assert_public_url

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "Quarterdeck"
CLIENT_VERSION = "1.0.0"
TIMEOUT_SECONDS = 20.0
# A server that answers with a library is not describable to an owner in one
# sitting, and a run cannot be told about it either.
MAX_TOOLS = 60
# Pages of tools asked for at most, so a server cannot keep the office paging.
MAX_PAGES = 20
MAX_BODY_BYTES = 2_000_000
# What one tool call may put into a prompt.
MAX_RESULT_CHARS = 40_000


class McpError(RuntimeError):
    """A server the office could not reach or could not understand.

    Carries a short code, never a credential and never a raw body.
    """

    def __init__(self, code: str, challenge: str = "") -> None:
        super().__init__(code)
        self.code = code
        # A server asking for sign-in says where to begin; kept for that only.
        self.challenge = challenge


@dataclass(frozen=True)
class OfferedTool:
    """One tool a server says it has.

    `claims` holds the server's own annotations. They are shown to the owner as
    what the server asserts, and are never read as permission.
    """

    name: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def claims_read_only(self) -> bool:
        """What the server says. Not what the office believes."""
        return bool(self.claims.get("readOnlyHint"))


@dataclass(frozen=True)
class Offering:
    server_name: str
    server_version: str
    protocol_version: str
    tools: list[OfferedTool]


def check_address(url: str, allow_list: list[str]) -> str:
    """The server's address, or a refusal. Public HTTPS and on the allow-list."""
    from app.tools.fetch_service import host_permitted

    try:
        checked = assert_public_url(url)
    except FetchError as refused:
        raise McpError(str(refused.args[0]) if refused.args else "address_refused") from None
    host = httpx.URL(checked).host
    if not host_permitted(host, allow_list):
        raise McpError("host_not_allowed")
    return checked


class McpClient:
    """A Streamable HTTP MCP client, for listing what a server offers."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def offering(self, url: str, token: str | None, allow_list: list[str]) -> Offering:
        """Complete the handshake and list the tools. Raises McpError."""
        if url.startswith(ACCOUNT_SCHEME):
            return _account_offering(url)
        address = check_address(url, allow_list)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if token:
            # Resolved from the secret store by the caller. It reaches the
            # server and nothing else: no prompt, no event, no log.
            headers["Authorization"] = f"Bearer {token}"
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            hello = await self._send(client, address, headers, "initialize", _handshake())
            session = hello.get("_session_id")
            if session:
                headers["Mcp-Session-Id"] = str(session)
            info = hello.get("serverInfo") or {}
            await self._notify(client, address, headers)
            tools = await self._all_tools(client, address, headers)
        finally:
            if self._client is None:
                await client.aclose()
        return Offering(
            server_name=str(info.get("name") or "unnamed server")[:128],
            server_version=str(info.get("version") or "")[:64],
            protocol_version=str(hello.get("protocolVersion") or "")[:32],
            tools=tools,
        )

    async def _all_tools(
        self, client: httpx.AsyncClient, address: str, headers: dict[str, str]
    ) -> list[OfferedTool]:
        """Every page of the server's tools, up to MAX_TOOLS.

        A server may list its tools a page at a time; the first page alone hid
        the rest. A cursor seen before would page for ever, so it is refused.
        """
        tools: list[OfferedTool] = []
        cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            listed = await self._send(
                client, address, headers, "tools/list", {"cursor": cursor} if cursor else {}
            )
            tools.extend(_tools_of(listed))
            if len(tools) >= MAX_TOOLS:
                return tools[:MAX_TOOLS]
            following = listed.get("nextCursor")
            if not isinstance(following, str) or not following:
                return tools
            if following in cursors:
                raise McpError("cursor_loop")
            cursors.add(following)
            cursor = following
        return tools

    async def call(
        self,
        url: str,
        token: str | None,
        allow_list: list[str],
        tool: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """Call one tool. Raises McpError; a tool's own failure is returned."""
        if url.startswith(ACCOUNT_SCHEME):
            return await _account_call(url, token, tool, arguments, self._client)
        address = check_address(url, allow_list)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            hello = await self._send(client, address, headers, "initialize", _handshake())
            session = hello.get("_session_id")
            if session:
                headers["Mcp-Session-Id"] = str(session)
            await self._notify(client, address, headers)
            result = await self._send(
                client,
                address,
                headers,
                "tools/call",
                {"name": tool, "arguments": arguments},
            )
        finally:
            if self._client is None:
                await client.aclose()
        return ToolResult(content=_content_of(result), failed=bool(result.get("isError")))

    async def _send(
        self,
        client: httpx.AsyncClient,
        address: str,
        headers: dict[str, str],
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
        try:
            # Streamed, so a server that keeps its event stream open, or sends
            # without end, is read only as far as the answer and the cap.
            async with client.stream("POST", address, headers=headers, json=body) as response:
                if response.status_code == 401:
                    # Without a key this is a server asking the owner to sign
                    # in; with one, the key (or an expired sign-in) was refused.
                    if "Authorization" not in headers:
                        raise McpError(
                            "sign_in_required", response.headers.get("WWW-Authenticate", "")
                        )
                    raise McpError("unauthorised")
                if response.status_code >= 400:
                    raise McpError(f"http_{response.status_code}")
                payload = await _read(response, method)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise McpError("unreachable") from None
        except httpx.HTTPError:
            # Sent, and no answer came back: whether the tool ran is not known
            # (`app/side_effects.py` keeps it from being sent twice).
            raise McpError("no_answer") from None
        if "error" in payload:
            # The server's own message is not carried: it is third-party text
            # and would end up in front of an owner or an agent.
            raise McpError("server_error")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpError("malformed_result")
        session = response.headers.get("Mcp-Session-Id")
        if session:
            result = {**result, "_session_id": session}
        return result

    async def _notify(
        self, client: httpx.AsyncClient, address: str, headers: dict[str, str]
    ) -> None:
        """Tell the server the handshake is complete, as the lifecycle requires."""
        try:
            await client.post(
                address,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        except httpx.HTTPError:
            raise McpError("unreachable") from None


# The owner's own Google or Microsoft account: tools the office runs
# itself against the company's API, not a third party's MCP server.
ACCOUNT_SCHEME = "account://"


def _account_offering(url: str) -> Offering:
    from app import accounts

    try:
        rows = accounts.offered(url)
    except accounts.AccountError as unknown:
        raise McpError(unknown.code) from None
    return Offering(
        server_name=accounts.account_of(url).server_name,
        server_version="",
        protocol_version="",
        tools=[
            OfferedTool(row["name"], row["description"], row["schema"], row["claims"])
            for row in rows
        ],
    )


async def _account_call(
    url: str,
    token: str | None,
    tool: str,
    arguments: dict[str, Any],
    client: httpx.AsyncClient | None,
) -> ToolResult:
    from app import accounts

    try:
        content, failed = await accounts.call(url, token, tool, arguments, client)
    except accounts.AccountError as refused:
        raise McpError(refused.code) from None
    return ToolResult(content=content, failed=failed)


@dataclass(frozen=True)
class ToolResult:
    """What a tool returned: text the office can read, and whether it failed.

    A server may mark its own result as an error; that is the tool failing,
    not the office. `content` is joined text only — a server's structured
    content, images and embedded resources are not carried into a prompt.
    """

    content: str
    failed: bool


def _handshake() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        # The office asks for nothing it cannot honour: no sampling (a server
        # running a model on the owner's keys) and no elicitation (a server
        # putting a question to the owner).
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
    }


async def _read(response: httpx.Response, request_id: str) -> dict[str, Any]:
    """The JSON-RPC answer to `request_id`, sent as JSON or as server-sent events.

    A stream may carry notifications, requests and other answers before ours,
    and an event's data may span several lines; it is read event by event and
    left as soon as our answer arrives. Either way no more than MAX_BODY_BYTES
    is read.
    """
    if "text/event-stream" not in response.headers.get("content-type", ""):
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BODY_BYTES:
                raise McpError("response_too_large")
        return _message(data.decode("utf-8", errors="replace"))
    seen = 0
    lines: list[str] = []
    async for line in response.aiter_lines():
        seen += len(line) + 1
        if seen > MAX_BODY_BYTES:
            raise McpError("response_too_large")
        if line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
            continue
        if line.strip() or not lines:
            continue
        # A blank line ends the event.
        found = _ours("\n".join(lines), request_id)
        lines = []
        if found is not None:
            return found
    if lines:
        found = _ours("\n".join(lines), request_id)
        if found is not None:
            return found
    raise McpError("empty_stream")


def _ours(text: str, request_id: str) -> dict[str, Any] | None:
    """An event's message if it answers our request; None for anything else."""
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("id") != request_id:
        return None
    return message


def _message(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except ValueError:
        raise McpError("not_json") from None
    if not isinstance(payload, dict):
        raise McpError("not_json")
    return payload


def _content_of(result: dict[str, Any]) -> str:
    """The text a tool returned, and nothing else.

    Only text blocks are carried. A server's images, audio and embedded
    resources are dropped rather than decoded: this text goes into a prompt,
    and the less of a third party's choosing that reaches one, the better.
    """
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    lines = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(line for line in lines if line)[:MAX_RESULT_CHARS]


def _tools_of(listed: dict[str, Any]) -> list[OfferedTool]:
    raw = listed.get("tools")
    if not isinstance(raw, list):
        raise McpError("malformed_tools")
    tools: list[OfferedTool] = []
    for item in raw[:MAX_TOOLS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        schema = item.get("inputSchema")
        claims = item.get("annotations")
        tools.append(
            OfferedTool(
                name=name[:128],
                description=str(item.get("description") or "")[:600],
                schema=schema if isinstance(schema, dict) else {},
                claims=claims if isinstance(claims, dict) else {},
            )
        )
    return tools
