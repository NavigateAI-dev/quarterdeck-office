"""Signing in to a tool server with the owner's own account.

Many vendors' MCP servers take no key: they ask the person to sign in and grant
access, as the MCP authorization spec describes. This is that flow, as a
public client that keeps its tokens in the office's secret store.

    1. The server answers 401 with a challenge naming its protected resource
       metadata (RFC 9728); without one, its well-known address is asked.
    2. That names the authorization server, whose metadata (RFC 8414, else
       OpenID discovery) gives the sign-in, token and registration addresses.
    3. The office registers itself (RFC 7591) as a public client whose only
       redirect is the office's own callback, on the address the owner uses.
    4. The owner's browser goes to the vendor's page with PKCE and the
       server as `resource` (RFC 8707); the vendor sends it back with a code.
    5. The code and its verifier become tokens, kept encrypted as the server's
       credential and refreshed shortly before they expire.

Every address in this flow comes from a third party, so each passes the same
public-HTTPS and allow-list checks as the server itself. A vendor's error text
is never carried; only a code is.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.mcp.client import McpError, check_address
from app.secret_store import SecretStore, SecretStoreError

TIMEOUT_SECONDS = 20
# Refreshed this long before it would expire, so a task does not start on a
# token that lapses in its first minute.
REFRESH_BEFORE_SECONDS = 300
CLIENT_NAME = "Quarterdeck"
CHALLENGE = re.compile(r'resource_metadata="([^"]+)"')


class OAuthError(Exception):
    """Signing in could not go ahead. Carries a code, never a vendor's text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AuthServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Registration:
    client_id: str
    client_secret: str | None
    auth_method: str


def _checked(url: str, allow_list: list[str]) -> str:
    try:
        return check_address(url, allow_list)
    except McpError as refused:
        raise OAuthError(refused.code) from None


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        raise OAuthError("sign_in_unreachable") from None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _well_known(base: str, name: str) -> list[str]:
    """Where a document named `name` lives for `base`, most specific first."""
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    places = [f"{origin}/.well-known/{name}{path}"] if path else []
    places.append(f"{origin}/.well-known/{name}")
    return places


def _client(given: httpx.AsyncClient | None) -> httpx.AsyncClient:
    return given or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)


async def discover(
    server_url: str,
    challenge: str,
    allow_list: list[str],
    client: httpx.AsyncClient | None = None,
) -> AuthServer:
    """Where and how to sign in to this server."""
    http = _client(client)
    try:
        named = CHALLENGE.search(challenge or "")
        places = [named.group(1)] if named else _well_known(server_url, "oauth-protected-resource")
        resource_meta = None
        for place in places:
            resource_meta = await _get_json(http, _checked(place, allow_list))
            if resource_meta:
                break
        if not resource_meta:
            # MCP 2025-03-26 had no such document: the server's own origin is
            # where its sign-in is described.
            parsed_server = urlparse(server_url)
            resource_meta = {
                "authorization_servers": [f"{parsed_server.scheme}://{parsed_server.netloc}"]
            }
        servers = resource_meta.get("authorization_servers") or []
        if not servers or not isinstance(servers[0], str):
            raise OAuthError("sign_in_unsupported")
        issuer = _checked(servers[0], allow_list)
        metadata = None
        parsed = urlparse(issuer)
        candidates = _well_known(issuer, "oauth-authorization-server") + _well_known(
            issuer, "openid-configuration"
        )
        if parsed.path.rstrip("/"):
            # OpenID discovery also allows the document after the issuer's path.
            candidates.append(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        for candidate in candidates:
            metadata = await _get_json(http, _checked(candidate, allow_list))
            if metadata:
                break
        if not metadata:
            raise OAuthError("sign_in_unsupported")
        methods = metadata.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise OAuthError("pkce_unsupported")
        authorize = metadata.get("authorization_endpoint")
        token = metadata.get("token_endpoint")
        if not isinstance(authorize, str) or not isinstance(token, str):
            raise OAuthError("sign_in_unsupported")
        registration = metadata.get("registration_endpoint")
        scopes = resource_meta.get("scopes_supported") or []
        return AuthServer(
            issuer=issuer,
            authorization_endpoint=_checked(authorize, allow_list),
            token_endpoint=_checked(token, allow_list),
            registration_endpoint=_checked(registration, allow_list)
            if isinstance(registration, str)
            else None,
            resource=str(resource_meta.get("resource") or server_url),
            scopes=tuple(str(scope) for scope in scopes if isinstance(scope, str)),
        )
    finally:
        if client is None:
            await http.aclose()


async def register(
    server: AuthServer,
    redirect_uri: str,
    allow_list: list[str],
    client: httpx.AsyncClient | None = None,
) -> Registration:
    """Register the office as a public client whose one redirect is its own."""
    if not server.registration_endpoint:
        raise OAuthError("registration_unsupported")
    http = _client(client)
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        response = await http.post(_checked(server.registration_endpoint, allow_list), json=body)
    except httpx.HTTPError:
        raise OAuthError("sign_in_unreachable") from None
    finally:
        if client is None:
            await http.aclose()
    if response.status_code not in (200, 201):
        raise OAuthError("registration_refused")
    try:
        issued = response.json()
    except ValueError:
        raise OAuthError("registration_refused") from None
    client_id = issued.get("client_id") if isinstance(issued, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError("registration_refused")
    secret = issued.get("client_secret")
    fallback = "client_secret_post" if secret else "none"
    method = str(issued.get("token_endpoint_auth_method") or fallback)
    return Registration(client_id, secret if isinstance(secret, str) else None, method)


def authorize_url(
    server: AuthServer, registration: Registration, redirect_uri: str
) -> tuple[str, str, str]:
    """The vendor's sign-in page, and the verifier and state to keep."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(32)
    query = {
        "response_type": "code",
        "client_id": registration.client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
        "state": state,
        "resource": server.resource,
    }
    if server.scopes:
        query["scope"] = " ".join(server.scopes)
    separator = "&" if "?" in server.authorization_endpoint else "?"
    return f"{server.authorization_endpoint}{separator}{urlencode(query)}", verifier, state


def credential(
    access: str,
    refresh: str | None,
    expires_at: float | None,
    registration: Registration,
    token_endpoint: str,
    resource: str,
    scope: str,
) -> str:
    """What is kept in the secret store for a signed-in server."""
    return json.dumps(
        {
            "kind": "oauth",
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": expires_at,
            "client_id": registration.client_id,
            "client_secret": registration.client_secret,
            "auth_method": registration.auth_method,
            "token_endpoint": token_endpoint,
            "resource": resource,
            "scope": scope,
        }
    )


async def _token_request(
    token_endpoint: str,
    fields: dict[str, str],
    registration: Registration,
    allow_list: list[str],
    client: httpx.AsyncClient | None,
    refused: str,
) -> dict[str, Any]:
    http = _client(client)
    data = {**fields, "client_id": registration.client_id}
    auth: tuple[str, str] | None = None
    if registration.client_secret and registration.auth_method == "client_secret_basic":
        auth = (registration.client_id, registration.client_secret)
    elif registration.client_secret:
        data["client_secret"] = registration.client_secret
    try:
        response = await http.post(
            _checked(token_endpoint, allow_list),
            data=data,
            headers={"Accept": "application/json"},
            auth=auth,  # type: ignore[arg-type]
        )
    except httpx.HTTPError:
        raise OAuthError("sign_in_unreachable") from None
    finally:
        if client is None:
            await http.aclose()
    if response.status_code != 200:
        raise OAuthError(refused)
    try:
        issued = response.json()
    except ValueError:
        raise OAuthError(refused) from None
    if not isinstance(issued, dict) or not isinstance(issued.get("access_token"), str):
        raise OAuthError(refused)
    return issued


def _expiry(issued: dict[str, Any]) -> float | None:
    lifetime = issued.get("expires_in")
    return time.time() + float(lifetime) if isinstance(lifetime, int | float) else None


async def exchange(
    server: AuthServer,
    registration: Registration,
    code: str,
    verifier: str,
    redirect_uri: str,
    allow_list: list[str],
    client: httpx.AsyncClient | None = None,
) -> str:
    """Turn the code the vendor sent back into a credential to keep."""
    issued = await _token_request(
        server.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": server.resource,
        },
        registration,
        allow_list,
        client,
        "sign_in_refused",
    )
    return credential(
        issued["access_token"],
        issued.get("refresh_token"),
        _expiry(issued),
        registration,
        server.token_endpoint,
        server.resource,
        str(issued.get("scope") or " ".join(server.scopes)),
    )


def is_oauth(raw: str) -> bool:
    if not raw.startswith("{"):
        return False
    try:
        return bool(json.loads(raw).get("kind") == "oauth")
    except (ValueError, AttributeError):
        return False


async def token_for(
    raw: str,
    allow_list: list[str],
    save: Callable[[str], None],
    client: httpx.AsyncClient | None = None,
) -> str:
    """The bearer token to send: a key as it is, or a current access token.

    An access token about to expire is refreshed, and the new credential saved.
    A refresh the vendor refuses means the owner must sign in again.
    """
    if not is_oauth(raw):
        return raw
    held = json.loads(raw)
    expires = held.get("expires_at")
    if not isinstance(expires, int | float) or expires - time.time() > REFRESH_BEFORE_SECONDS:
        return str(held["access_token"])
    if not held.get("refresh_token"):
        raise OAuthError("sign_in_required")
    registration = Registration(
        str(held["client_id"]), held.get("client_secret"), str(held.get("auth_method") or "none")
    )
    try:
        issued = await _token_request(
            str(held["token_endpoint"]),
            {
                "grant_type": "refresh_token",
                "refresh_token": str(held["refresh_token"]),
                "resource": str(held.get("resource") or ""),
            },
            registration,
            allow_list,
            client,
            "sign_in_required",
        )
    except OAuthError as failed:
        raise OAuthError("sign_in_required") from failed
    save(
        credential(
            issued["access_token"],
            issued.get("refresh_token") or held["refresh_token"],
            _expiry(issued),
            registration,
            str(held["token_endpoint"]),
            str(held.get("resource") or ""),
            str(issued.get("scope") or held.get("scope") or ""),
        )
    )
    return str(issued["access_token"])


class Flows:
    """Sign-ins in progress: each redeemed once, and only for a short while.

    They are kept in the encrypted secret store, not in memory, so the owner can
    be at the vendor's page while the office restarts. An entry is named by a
    hash of its state, so the store never holds the state itself, and it holds
    the PKCE verifier and any client secret only encrypted.
    """

    def __init__(
        self,
        open_store: Callable[[], SecretStore],
        ttl_seconds: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.open_store = open_store
        self.ttl = ttl_seconds
        self.clock = clock

    def start(self, data: dict[str, Any], state: str | None = None) -> str:
        store = self.open_store()
        now = self.clock()
        # Forgotten ones are dropped as new ones start.
        for name in store.names(SIGN_IN_PREFIX):
            if self._lapsed(store, name, now):
                store.delete(name)
        key = state or secrets.token_urlsafe(32)
        store.put(_sign_in_name(key), json.dumps({"started": now, "data": data}))
        return key

    def take(self, state: str) -> dict[str, Any] | None:
        name = _sign_in_name(state)
        try:
            store = self.open_store()
            held = json.loads(store.get(name))
        except (SecretStoreError, ValueError):
            return None
        store.delete(name)
        if self.clock() - float(held.get("started", 0)) > self.ttl:
            return None
        data: dict[str, Any] = held.get("data") or {}
        return data

    def _lapsed(self, store: SecretStore, name: str, now: float) -> bool:
        try:
            started = float(json.loads(store.get(name)).get("started", 0))
        except (SecretStoreError, ValueError, AttributeError):
            return True
        return now - started > self.ttl


SIGN_IN_PREFIX = "oauth-sign-in-"


def _sign_in_name(state: str) -> str:
    return SIGN_IN_PREFIX + hashlib.sha256(state.encode()).hexdigest()


def pending(server: AuthServer, registration: Registration) -> dict[str, Any]:
    """What a sign-in keeps of the vendor and the office's registration there."""
    return {"server": asdict(server), "registration": asdict(registration)}


def held_server(held: dict[str, Any]) -> AuthServer:
    kept = dict(held["server"])
    return AuthServer(**{**kept, "scopes": tuple(kept.get("scopes") or ())})


def held_registration(held: dict[str, Any]) -> Registration:
    return Registration(**held["registration"])
