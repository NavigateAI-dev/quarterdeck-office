"""Signing in with a name and a password the owner set.

The owner key and the launcher's one-time codes are 256-bit random values; a
password is not, so it is kept only as a salted scrypt hash, compared in
constant time, and guessing it is stopped outright (see the stricter
throttle in `app.api.sessions`), not merely slowed. Changing or removing it
asks for the current one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OfficeSettings

MIN_PASSWORD = 10
# scrypt's cost: about 16 MB and a few tenths of a second per check.
COST = {"n": 2**14, "r": 8, "p": 1}


class LoginRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024**2
    )


def _row(session: Session) -> OfficeSettings | None:
    return session.scalar(select(OfficeSettings).limit(1))


def held(session: Session) -> dict[str, Any]:
    row = _row(session)
    return dict(getattr(row, "owner_login_json", None) or {})


def is_set(session: Session) -> bool:
    return bool(held(session).get("hash"))


def name_of(session: Session) -> str:
    return str(held(session).get("name") or "")


def check(session: Session, name: str, password: str) -> bool:
    """Whether this name and password are the owner's. Always does the work."""
    stored = held(session)
    salt = base64.b64decode(stored["salt"]) if stored.get("salt") else b"no-password-set"
    cost = {key: int(stored.get(key) or COST[key]) for key in COST}
    expected = base64.b64decode(stored["hash"]) if stored.get("hash") else b"\0" * 32
    # Hashed even with nothing set, so the answer takes as long either way.
    given = _hash(password, salt, **cost)
    right_name = hmac.compare_digest(name.strip().lower(), str(stored.get("name") or "").lower())
    return bool(stored.get("hash")) and right_name and hmac.compare_digest(given, expected)


def set_login(session: Session, name: str, password: str, current: str = "") -> None:
    """Set or change the sign-in. With one set already, its password is asked."""
    name = name.strip()
    if not name or len(name) > 120:
        raise LoginRejected("login_name_invalid")
    if len(password) < MIN_PASSWORD:
        raise LoginRejected("password_too_short")
    if is_set(session) and not check(session, name_of(session), current):
        raise LoginRejected("current_password_wrong")
    salt = secrets.token_bytes(16)
    stored = {
        "name": name,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_hash(password, salt, **COST)).decode(),
        **COST,
        "set_at": datetime.now(UTC).isoformat(),
    }
    row = _row(session)
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    row.owner_login_json = stored
    session.flush()


def clear_login(session: Session, current: str) -> None:
    if not is_set(session):
        return
    if not check(session, name_of(session), current):
        raise LoginRejected("current_password_wrong")
    row = _row(session)
    if row is not None:
        row.owner_login_json = {}
        session.flush()
