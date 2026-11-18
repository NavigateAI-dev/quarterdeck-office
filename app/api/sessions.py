"""Owner browser sessions.

The browser exchanges the owner token once for an HttpOnly, SameSite=Strict
session cookie, so the token itself never has to live in page memory. Only a
SHA-256 hash of the cookie is stored. The CSRF token is an HMAC of the cookie
secret, so it can be re-derived after a reload without storing it. A write
authenticated by cookie must carry that CSRF token and a same-origin Origin.
Bearer tokens remain valid for scripts, which carry no ambient credential.
"""

import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app import owner_login
from app.access import confirm_phone, host_of, trusted_origins
from app.config import Settings
from app.db.models import OwnerSession

SESSION_COOKIE = "office_session"
CSRF_HEADER = "X-CSRF-Token"
IDLE_TIMEOUT = timedelta(hours=12)
ABSOLUTE_LIFETIME = timedelta(days=7)
# "Keep me signed in on this device": a month, idle or not.
REMEMBERED_LIFETIME = timedelta(days=30)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return hmac.new(token.encode(), b"quarterdeck-csrf-v1", hashlib.sha256).hexdigest()


class LoginThrottle:
    """Wrong guesses, slowed: per address, and a ceiling across all of them.

    It only ever slows failures. A right credential always gets in, so a
    device on the home network failing on purpose cannot keep the owner out;
    the owner token and the one-time codes are 256-bit random values, which
    no rate of guessing finds. Behind Docker every outside address can look
    the same, which is why getting in never depends on this count.
    """

    def __init__(
        self,
        limit: int = 5,
        overall: int = 50,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.overall = overall
        self.window = window
        self.clock = clock
        self.by_address: dict[str, deque[float]] = {}
        self.everyone: deque[float] = deque()

    def _trim(self, failures: deque[float]) -> None:
        cutoff = self.clock() - self.window
        while failures and failures[0] < cutoff:
            failures.popleft()

    def blocked(self, address: str = "") -> bool:
        self._trim(self.everyone)
        mine = self.by_address.get(address)
        if mine is not None:
            self._trim(mine)
            if not mine:
                self.by_address.pop(address, None)
        return len(self.everyone) >= self.overall or (mine is not None and len(mine) >= self.limit)

    def fail(self, address: str = "") -> None:
        now = self.clock()
        self.everyone.append(now)
        self.by_address.setdefault(address, deque()).append(now)


class PasswordThrottle(LoginThrottle):
    """Guessing a password is stopped, not slowed.

    Unlike the owner key, a password can be guessed, so once an address (or
    everyone together) has failed too often, even the right password is
    refused until the window passes. The owner key and the launcher's codes
    are counted apart and keep working, so the owner is never locked out.
    """

    def __init__(
        self,
        limit: int = 5,
        overall: int = 20,
        window: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(limit=limit, overall=overall, window=window, clock=clock)


def served_over_https(session_factory: sessionmaker[Session], host: str) -> bool:
    """Whether this host is a verified access point the office serves over HTTPS.

    Decided by the office's own record of where it may be reached, never by a
    forwarded header: Serve hands requests on over plain HTTP inside the box.
    """
    if not host:
        return False
    with session_factory() as session:
        origins = trusted_origins(session)
    wanted = host_of(host)
    return any(
        origin.startswith("https://") and host_of(urlsplit(origin).netloc) == wanted
        for origin in origins
    )


def peer_of(request: Request) -> str:
    """The connecting address as the server saw it; never a forwarded header."""
    return request.client.host if request.client is not None else ""


CODE_TTL = 120.0


class LoginCodes:
    """Single-use sign-in codes minted by the local launcher.

    Held in memory only: a restart invalidates outstanding codes, which is the
    safe failure. Only hashes are kept.
    """

    def __init__(self) -> None:
        self.codes: dict[str, float] = {}

    def mint(self) -> str:
        now = time.monotonic()
        self.codes = {key: expiry for key, expiry in self.codes.items() if expiry > now}
        code = secrets.token_urlsafe(32)
        self.codes[token_hash(code)] = now + CODE_TTL
        return code

    def redeem(self, code: str) -> bool:
        expiry = self.codes.pop(token_hash(code), None)
        return expiry is not None and expiry > time.monotonic()


class PasswordSignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=1, max_length=256)
    remember: bool = False


class SessionOpen(BaseModel):
    """Exactly one of: the owner token, or a one-time code from the launcher."""

    token: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    code: SecretStr | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def exactly_one(self) -> "SessionOpen":
        if (self.token is None) == (self.code is None):
            raise ValueError("provide exactly one of token or code")
        return self


def lookup(session_factory: sessionmaker[Session], raw: str) -> OwnerSession | None:
    """Return the live session for a cookie value, refreshing its idle clock."""
    now = datetime.now(UTC)
    with session_factory() as session, session.begin():
        row = session.scalar(select(OwnerSession).where(OwnerSession.token_hash == token_hash(raw)))
        if (
            row is None
            or row.revoked_at is not None
            or row.expires_at <= now
            or row.last_seen_at + (REMEMBERED_LIFETIME if row.remember else IDLE_TIMEOUT) <= now
        ):
            return None
        row.last_seen_at = now
        return row


def same_origin(request: Request, origin: str) -> bool:
    """The request's own origin, or an outside address the owner set up and the
    office verified, under its own host. Forwarding headers are not
    trusted: the remote proxy does not document them."""
    if origin == f"{request.url.scheme}://{request.url.netloc}":
        return True
    reachable = getattr(request.app.state, "reachable", None)
    netloc = request.headers.get("host", request.url.netloc)
    return reachable is not None and bool(reachable.trusts(origin, netloc))


def check_write(request: Request, raw: str) -> None:
    """Cookie-authenticated writes need a same-origin request and the CSRF token."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None or not same_origin(request, origin):
        raise HTTPException(status_code=403, detail="origin_rejected")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied:
        raise HTTPException(status_code=403, detail="csrf_required")
    if not secrets.compare_digest(supplied, csrf_for(raw)):
        raise HTTPException(status_code=403, detail="csrf_invalid")


def revoke_all(session_factory: sessionmaker[Session]) -> int:
    """Recovery: end every owner session."""
    with session_factory() as session, session.begin():
        result = session.execute(
            update(OwnerSession)
            .where(OwnerSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def build_session_router(
    session_factory: sessionmaker[Session],
    settings: Settings,
    throttle: LoginThrottle | None = None,
    codes: LoginCodes | None = None,
    passwords: PasswordThrottle | None = None,
) -> APIRouter:
    router = APIRouter()
    limiter = throttle or LoginThrottle()
    login_codes = codes or LoginCodes()
    password_guesses = passwords or PasswordThrottle()

    def start(request: Request, response: Response, remember: bool = False) -> dict[str, str]:
        """A new owner session on this device, as an HttpOnly cookie."""
        raw = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        lifetime = REMEMBERED_LIFETIME if remember else ABSOLUTE_LIFETIME
        with session_factory() as session, session.begin():
            session.add(
                OwnerSession(
                    token_hash=token_hash(raw),
                    last_seen_at=now,
                    expires_at=now + lifetime,
                    remember=remember,
                )
            )
        response.set_cookie(
            SESSION_COOKIE,
            raw,
            max_age=int(lifetime.total_seconds()),
            httponly=True,
            samesite="strict",
            path="/",
            # Over the verified HTTPS address (Tailscale) the browser only ever
            # sends it encrypted; localhost and the home network are HTTP.
            secure=served_over_https(session_factory, request.headers.get("host", "")),
        )
        return {"csrf_token": csrf_for(raw), "owner": settings.owner_name}

    @router.get("/session/methods")
    def sign_in_methods() -> dict[str, bool]:
        """What the sign-in page offers. Says only whether a password is set."""
        with session_factory() as session:
            return {"password": owner_login.is_set(session)}

    @router.post("/session/password")
    def open_with_password(
        body: PasswordSignIn, request: Request, response: Response
    ) -> dict[str, str]:
        origin = request.headers.get("origin")
        if origin is not None and not same_origin(request, origin):
            raise HTTPException(status_code=403, detail="origin_rejected")
        peer = peer_of(request)
        if password_guesses.blocked(peer):
            # Refused even when right: a password can be guessed.
            raise HTTPException(status_code=429, detail="too_many_attempts")
        with session_factory() as session:
            valid = owner_login.check(session, body.name, body.password.get_secret_value())
        if not valid:
            password_guesses.fail(peer)
            raise HTTPException(status_code=401, detail="sign_in_wrong")
        return start(request, response, remember=body.remember)

    @router.post("/session/code")
    def mint_code(request: Request) -> dict[str, str]:
        """For the local launcher, which holds the owner token, to open the
        browser already signed in without the owner handling the token."""
        expected = settings.owner_api_token
        if expected is None:
            raise HTTPException(status_code=503, detail="owner_token_not_configured")
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            supplied, expected.get_secret_value()
        ):
            raise HTTPException(status_code=401, detail="Owner authentication required")
        return {"code": login_codes.mint(), "expires_in": str(int(CODE_TTL))}

    @router.post("/session")
    def open_session(body: SessionOpen, request: Request, response: Response) -> dict[str, str]:
        origin = request.headers.get("origin")
        if origin is not None and not same_origin(request, origin):
            raise HTTPException(status_code=403, detail="origin_rejected")
        expected = settings.owner_api_token
        if expected is None:
            raise HTTPException(status_code=503, detail="owner_token_not_configured")
        if body.code is not None:
            valid = login_codes.redeem(body.code.get_secret_value())
        else:
            assert body.token is not None  # noqa: S101 - guaranteed by the model
            valid = secrets.compare_digest(
                body.token.get_secret_value(), expected.get_secret_value()
            )
        if not valid:
            # Only a wrong guess is counted or slowed; a right one always gets in.
            peer = peer_of(request)
            slowed = limiter.blocked(peer)
            limiter.fail(peer)
            if slowed:
                raise HTTPException(status_code=429, detail="too_many_attempts")
            raise HTTPException(status_code=401, detail="Owner authentication required")
        if body.code is not None:
            # A pairing code redeemed under an outside address: a phone really
            # reached the office there. A local sign-in records nothing.
            confirm_phone(
                session_factory,
                request.headers.get("host", ""),
                request.headers.get("user-agent", ""),
            )
        return start(request, response)

    @router.get("/session")
    def current_session(request: Request) -> dict[str, str]:
        raw = request.cookies.get(SESSION_COOKIE, "")
        if not raw or lookup(session_factory, raw) is None:
            raise HTTPException(status_code=401, detail="Owner authentication required")
        return {"csrf_token": csrf_for(raw), "owner": settings.owner_name}

    @router.delete("/session", status_code=204)
    def close_session(request: Request, response: Response) -> None:
        raw = request.cookies.get(SESSION_COOKIE, "")
        if raw:
            check_write(request, raw)
            with session_factory() as session, session.begin():
                session.execute(
                    update(OwnerSession)
                    .where(OwnerSession.token_hash == token_hash(raw))
                    .values(revoked_at=datetime.now(UTC))
                )
        response.delete_cookie(SESSION_COOKIE, path="/")

    return router
