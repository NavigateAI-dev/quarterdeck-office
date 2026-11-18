"""Opening the office from a phone.

The office answers on the Mac itself (localhost) and, once the owner sets them
up, at two outside addresses: the home network and the private remote route.
An outside address counts only once the office has verified it, and only for
its own host: a request under any other host is refused, which also closes the
door to DNS rebinding. The same-origin checks accept those addresses, so a
phone's writes pass; nothing is trusted from forwarding headers, which the
remote proxy does not document.

A phone signs in with a pairing link: the address and a one-time code
(single use, two minutes), drawn as a QR code by the office itself. A phone
that opens it is recorded as confirmed; showing the QR proves nothing.
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AccessPoint

KINDS = ("home", "remote")
# This running office, so a probe can tell it reached this office and not
# something else listening at that address.
OFFICE_ID = secrets.token_hex(8)
PROBE_SECONDS = 4.0
# Always answered: the Mac itself, and the test client's host.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "test", "testserver"})
CACHE_SECONDS = 5.0


def host_of(netloc: str) -> str:
    """The host part of a Host header or netloc, without the port."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def origin_of(address: str) -> str:
    parts = urlsplit(address.strip())
    return f"{parts.scheme}://{parts.netloc}".lower()


def valid_address(address: str) -> str | None:
    """Why an address cannot be a phone's way in, or None when it can."""
    parts = urlsplit(address.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Give the full address, starting http:// or https://."
    host = parts.hostname.lower()
    if host in LOCAL_HOSTS or host.startswith("127."):
        return "That is this computer's own address; a phone cannot reach it."
    if re.fullmatch(r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+", host):
        return "That looks like a Docker-internal address; use the Mac's own network address."
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "Give only the address, without a path."
    return None


class Reachable:
    """The verified outside addresses, cached briefly for every request."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.read_at = 0.0
        self.origins: set[str] = set()

    def refresh(self) -> None:
        self.read_at = 0.0

    def current(self) -> set[str]:
        if time.monotonic() - self.read_at > CACHE_SECONDS:
            with self.session_factory() as session:
                self.origins = trusted_origins(session)
            self.read_at = time.monotonic()
        return self.origins

    def allows_host(self, netloc: str) -> bool:
        host = host_of(netloc)
        if host in LOCAL_HOSTS or host.startswith("127."):
            return True
        return any(host_of(urlsplit(origin).netloc) == host for origin in self.current())

    def trusts(self, origin: str, netloc: str) -> bool:
        """An outside origin is trusted only under its own host."""
        origin = origin.lower()
        return origin in self.current() and host_of(urlsplit(origin).netloc) == host_of(netloc)


def trusted_origins(session: Session) -> set[str]:
    return {
        origin_of(point.address)
        for point in session.scalars(select(AccessPoint).where(AccessPoint.enabled))
        if point.address and point.verified_at is not None
    }


def point(session: Session, kind: str) -> AccessPoint:
    found = session.get(AccessPoint, kind)
    if found is None:
        found = AccessPoint(kind=kind, enabled=False, check_json={})
        session.add(found)
        session.flush()
    return found


def device_of(user_agent: str) -> str:
    """A phone in a few words, from its user agent: never the whole string."""
    said = user_agent or ""
    kind = next(
        (
            name
            for key, name in (
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Android", "Android phone"),
                ("Macintosh", "Mac"),
                ("Windows", "Windows PC"),
            )
            if key in said
        ),
        "a device",
    )
    browser = next(
        (
            name
            for key, name in (
                ("CriOS", "Chrome"),
                ("FxiOS", "Firefox"),
                ("EdgiOS", "Edge"),
                ("Chrome", "Chrome"),
                ("Firefox", "Firefox"),
                ("Safari", "Safari"),
            )
            if key in said
        ),
        "",
    )
    return f"{kind} · {browser}" if browser else kind


def confirm_phone(
    session_factory: sessionmaker[Session], netloc: str, user_agent: str
) -> str | None:
    """A sign-in by pairing code under an outside address: a phone reached the
    office there. Returns the kind confirmed, or None for a local sign-in."""
    host = host_of(netloc)
    if host in LOCAL_HOSTS or host.startswith("127."):
        return None
    with session_factory() as session, session.begin():
        for found in session.scalars(select(AccessPoint).where(AccessPoint.enabled)):
            if found.address and host_of(urlsplit(found.address).netloc) == host:
                found.confirmed_at = datetime.now(UTC)
                found.confirmed_device = device_of(user_agent)[:120]
                return found.kind
    return None


def qr_svg(text: str) -> str:
    """A QR code drawn by the office itself: nothing leaves the machine."""
    import segno

    return str(segno.make(text, error="m").svg_inline(scale=6, dark="#1f2a24", light="#ffffff"))


def view(found: AccessPoint) -> dict[str, Any]:
    return {
        "kind": found.kind,
        "address": found.address,
        "enabled": found.enabled,
        "verified_at": found.verified_at.isoformat() if found.verified_at else None,
        "last_check_at": found.last_check_at.isoformat() if found.last_check_at else None,
        "check": dict(found.check_json or {}),
        "confirmed_at": found.confirmed_at.isoformat() if found.confirmed_at else None,
        "confirmed_device": found.confirmed_device,
    }


def home_setup(settings: Any) -> dict[str, Any]:
    """Whether the one-time host step for the home network is in effect."""
    bind = str(getattr(settings, "office_bind", "") or "127.0.0.1").strip()
    suggested = [
        part.strip()
        for part in str(getattr(settings, "office_lan_addresses", "") or "").split(",")
        if part.strip()
    ]
    return {
        "required": bind in ("127.0.0.1", "localhost", "::1", ""),
        "bind": bind,
        "suggested": suggested,
        "command": "python3 -m scripts.access home",
        "undo": "python3 -m scripts.access home --off",
    }


async def probe(address: str, host: str | None = None) -> dict[str, Any]:
    """Ask the address whether this office answers there. `host` is the name the
    phone will use, when it differs from the address connected to."""
    import httpx

    nonce = secrets.token_hex(8)
    headers = {"Host": host} if host else {}
    checked = {"via": address, "host": host, "at": datetime.now(UTC).isoformat()}
    try:
        async with httpx.AsyncClient(timeout=PROBE_SECONDS, follow_redirects=False) as client:
            response = await client.get(
                f"{origin_of(address)}/access/probe/{nonce}", headers=headers
            )
        body = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as failed:
        return {**checked, "answered": False, "reason": type(failed).__name__}
    if body.get("office") == OFFICE_ID and body.get("nonce") == nonce:
        return {**checked, "answered": True}
    if response.status_code == 200:
        return {**checked, "answered": False, "reason": "another service answered there"}
    return {**checked, "answered": False, "reason": f"answered {response.status_code}"}
