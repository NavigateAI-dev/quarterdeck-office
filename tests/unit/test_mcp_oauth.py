"""Signing in to a tool server with the owner's account."""

import base64
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.mcp import oauth
from app.mcp.client import McpClient, McpError
from app.secret_store import SecretStore, generate_root_key, load_root_key

SERVER = "https://mcp.example.com/mcp"
PRM = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
ISSUER = "https://auth.example.com"
CHALLENGE = f'Bearer realm="OAuth", resource_metadata="{PRM}"'


@pytest.fixture(autouse=True)
def resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin DNS: these exercise the address checks, not the network."""
    import socket

    def resolved(host: str, port: int, *args: object, **kwargs: object) -> list:
        address = "127.0.0.1" if host in ("localhost", "127.0.0.1") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolved)


class Vendor:
    """A sign-in service as the MCP spec describes it, recording what it was sent."""

    def __init__(self, **over: Any) -> None:
        self.seen: list[httpx.Request] = []
        self.metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "registration_endpoint": f"{ISSUER}/register",
            "code_challenge_methods_supported": ["S256"],
            **over.get("metadata", {}),
        }
        self.prm = {
            "resource": SERVER,
            "authorization_servers": [ISSUER],
            "scopes_supported": ["read", "write"],
            **over.get("prm", {}),
        }
        self.token_status = over.get("token_status", 200)
        self.issued = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = str(request.url).split("?")[0]
        if url == PRM or url == "https://mcp.example.com/.well-known/oauth-protected-resource":
            return httpx.Response(200, json=self.prm)
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=self.metadata)
        if url == f"{ISSUER}/register":
            return httpx.Response(201, json={"client_id": "office-client"})
        if url == f"{ISSUER}/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            self.issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{self.issued}",
                    "refresh_token": f"rt-{self.issued}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# The server says it needs sign-in.


async def test_a_401_without_a_key_asks_for_sign_in_and_keeps_the_challenge() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})

    client = McpClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(McpError) as asked:
        await client.offering(SERVER, None, [])
    assert asked.value.code == "sign_in_required"
    assert asked.value.challenge == CHALLENGE


async def test_a_401_with_a_key_is_still_a_refused_key() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": CHALLENGE})

    client = McpClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(McpError) as refused:
        await client.offering(SERVER, "sk-wrong", [])
    assert refused.value.code == "unauthorised"


# Discovery.


async def test_discovery_follows_the_servers_challenge() -> None:
    vendor = Vendor()
    found = await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    assert found.authorization_endpoint == f"{ISSUER}/authorize"
    assert found.token_endpoint == f"{ISSUER}/token"
    assert found.resource == SERVER
    assert found.scopes == ("read", "write")
    assert str(vendor.seen[0].url) == PRM


async def test_without_a_challenge_the_well_known_address_is_asked() -> None:
    vendor = Vendor()
    found = await oauth.discover(SERVER, "", [], vendor.client())
    assert found.registration_endpoint == f"{ISSUER}/register"
    assert str(vendor.seen[0].url) == PRM


async def test_an_issuer_with_a_path_is_asked_at_its_inserted_address() -> None:
    issuer = "https://auth.example.com/tenant"
    vendor = Vendor(prm={"authorization_servers": [issuer]}, metadata={"issuer": issuer})
    asked: list[str] = []
    inner = vendor.handle

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if (
            str(request.url)
            == "https://auth.example.com/.well-known/oauth-authorization-server/tenant"
        ):
            return httpx.Response(200, json=vendor.metadata)
        return inner(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    found = await oauth.discover(SERVER, CHALLENGE, [], client)
    assert found.token_endpoint == f"{ISSUER}/token"
    assert "https://auth.example.com/.well-known/oauth-authorization-server/tenant" in asked


async def test_a_server_on_the_older_spec_signs_in_at_its_own_origin() -> None:
    """MCP 2025-03-26: no protected-resource document; the server's origin is
    the sign-in service (Atlassian and Intercom, as checked)."""
    metadata = {
        "issuer": "https://mcp.example.com",
        "authorization_endpoint": "https://mcp.example.com/authorize",
        "token_endpoint": "https://mcp.example.com/token",
        "registration_endpoint": "https://mcp.example.com/register",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://mcp.example.com/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    found = await oauth.discover(SERVER, 'Bearer realm="OAuth"', [], client)
    assert found.authorization_endpoint == "https://mcp.example.com/authorize"
    assert found.registration_endpoint == "https://mcp.example.com/register"
    assert found.resource == SERVER and found.scopes == ()


async def test_a_sign_in_address_that_is_not_public_https_is_refused() -> None:
    vendor = Vendor(prm={"authorization_servers": ["http://127.0.0.1:9000"]})
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    assert refused.value.code in ("scheme_not_allowed", "non_public_address")


async def test_a_sign_in_host_outside_the_allow_list_is_refused() -> None:
    vendor = Vendor()
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth.discover(SERVER, CHALLENGE, ["mcp.example.com"], vendor.client())
    assert refused.value.code == "host_not_allowed"


async def test_a_service_without_pkce_s256_is_refused() -> None:
    vendor = Vendor(metadata={"code_challenge_methods_supported": ["plain"]})
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    assert refused.value.code == "pkce_unsupported"


# Registration and the sign-in page.


async def test_the_office_registers_as_a_public_client_with_its_own_address() -> None:
    vendor = Vendor()
    found = await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    registration = await oauth.register(
        found, "http://127.0.0.1:8000/mcp/oauth/callback", [], vendor.client()
    )
    assert registration.client_id == "office-client"
    sent = json.loads(vendor.seen[-1].content)
    assert sent["redirect_uris"] == ["http://127.0.0.1:8000/mcp/oauth/callback"]
    assert sent["token_endpoint_auth_method"] == "none"
    assert "authorization_code" in sent["grant_types"]


async def test_a_service_without_registration_cannot_be_signed_in_to() -> None:
    vendor = Vendor(metadata={"registration_endpoint": None})
    found = await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth.register(found, "http://127.0.0.1:8000/mcp/oauth/callback", [], vendor.client())
    assert refused.value.code == "registration_unsupported"


async def test_the_sign_in_page_carries_pkce_state_and_the_resource() -> None:
    vendor = Vendor()
    found = await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    registration = oauth.Registration("office-client", None, "none")
    page, verifier, state = oauth.authorize_url(
        found, registration, "http://127.0.0.1:8000/mcp/oauth/callback"
    )
    query = {k: v[0] for k, v in parse_qs(urlparse(page).query).items()}
    assert page.startswith(f"{ISSUER}/authorize?")
    assert query["response_type"] == "code"
    assert query["client_id"] == "office-client"
    assert query["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert query["code_challenge"] == expected.decode()
    assert query["state"] == state and len(state) >= 32
    assert query["resource"] == SERVER
    assert query["scope"] == "read write"


# Tokens.


async def test_the_code_is_exchanged_with_its_verifier() -> None:
    vendor = Vendor()
    found = await oauth.discover(SERVER, CHALLENGE, [], vendor.client())
    registration = oauth.Registration("office-client", None, "none")
    credential = await oauth.exchange(
        found, registration, "the-code", "the-verifier", "http://x/cb", [], vendor.client()
    )
    sent = form(vendor.seen[-1])
    assert sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"] == "the-verifier" and sent["resource"] == SERVER
    stored = json.loads(credential)
    assert stored["kind"] == "oauth" and stored["access_token"] == "at-1"
    assert stored["expires_at"] > time.time() + 3000


async def test_a_token_is_used_as_it_is_until_it_is_about_to_expire() -> None:
    vendor = Vendor()
    saved: list[str] = []
    credential = oauth.credential(
        "at-0",
        "rt-0",
        time.time() + 3600,
        oauth.Registration("c", None, "none"),
        f"{ISSUER}/token",
        SERVER,
        "",
    )
    token = await oauth.token_for(credential, [], saved.append, vendor.client())
    assert token == "at-0" and saved == [] and vendor.seen == []


async def test_a_token_about_to_expire_is_refreshed_and_kept() -> None:
    vendor = Vendor()
    saved: list[str] = []
    credential = oauth.credential(
        "at-0",
        "rt-0",
        time.time() + 30,
        oauth.Registration("c", None, "none"),
        f"{ISSUER}/token",
        SERVER,
        "",
    )
    token = await oauth.token_for(credential, [], saved.append, vendor.client())
    assert token == "at-1"
    assert form(vendor.seen[-1])["grant_type"] == "refresh_token"
    assert json.loads(saved[-1])["access_token"] == "at-1"


async def test_a_refused_refresh_means_signing_in_again() -> None:
    vendor = Vendor(token_status=400)
    credential = oauth.credential(
        "at-0",
        "rt-0",
        time.time() - 10,
        oauth.Registration("c", None, "none"),
        f"{ISSUER}/token",
        SERVER,
        "",
    )
    with pytest.raises(oauth.OAuthError) as expired:
        await oauth.token_for(credential, [], lambda _: None, vendor.client())
    assert expired.value.code == "sign_in_required"


async def test_a_plain_key_passes_through_untouched() -> None:
    assert await oauth.token_for("sk-plain-key", [], lambda _: None) == "sk-plain-key"


# The sign-in in progress.


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path) -> Callable[[], SecretStore]:
    key = tmp_path / "root.key"
    generate_root_key(key)
    return lambda: SecretStore(tmp_path / "secrets.json", load_root_key(key))


def test_a_sign_in_is_redeemed_once(tmp_path: Path) -> None:
    flows = oauth.Flows(_store(tmp_path))
    state = flows.start({"server_id": "s1"})
    assert flows.take(state) == {"server_id": "s1"}
    assert flows.take(state) is None


def test_a_sign_in_lapses_after_its_time(tmp_path: Path) -> None:
    clock = Clock()
    flows = oauth.Flows(_store(tmp_path), ttl_seconds=600, clock=clock)
    state = flows.start({"server_id": "s1"})
    clock.now += 601
    assert flows.take(state) is None


def test_a_sign_in_survives_a_restart(tmp_path: Path) -> None:
    """Found in the docs review: sign-ins were held in memory, so restarting the
    office while the owner was at the vendor's page lost the sign-in."""
    opener = _store(tmp_path)
    state = oauth.Flows(opener).start({"server_id": "s1", "verifier": "v-123"})
    assert oauth.Flows(opener).take(state) == {"server_id": "s1", "verifier": "v-123"}


def test_a_sign_in_is_kept_encrypted_and_not_under_its_state(tmp_path: Path) -> None:
    flows = oauth.Flows(_store(tmp_path))
    state = flows.start({"server_id": "s1", "verifier": "verifier-in-the-clear"})
    kept = (tmp_path / "secrets.json").read_text()
    assert "verifier-in-the-clear" not in kept
    assert state not in kept


def test_lapsed_sign_ins_are_cleared_as_new_ones_start(tmp_path: Path) -> None:
    clock = Clock()
    opener = _store(tmp_path)
    flows = oauth.Flows(opener, ttl_seconds=600, clock=clock)
    flows.start({"server_id": "old"})
    clock.now += 601
    flows.start({"server_id": "new"})
    names = json.loads((tmp_path / "secrets.json").read_text())["secrets"]
    assert len(names) == 1


def test_a_sign_in_carries_the_vendor_and_registration(tmp_path: Path) -> None:
    server = oauth.AuthServer("i", "a", "t", None, "r", ("read", "write"))
    registration = oauth.Registration("client", "secret", "client_secret_post")
    flows = oauth.Flows(_store(tmp_path))
    state = flows.start(oauth.pending(server, registration))
    held = flows.take(state)
    assert held is not None
    assert oauth.held_server(held) == server
    assert oauth.held_registration(held) == registration
