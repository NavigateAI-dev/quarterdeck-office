import secrets

from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from app.api.sessions import SESSION_COOKIE, LoginThrottle, check_write, lookup, peer_of
from app.config import Settings


class OwnerIdentity:
    """The single authenticated owner (SPEC sections 14, 19, 25).

    Identity is derived here, never taken from a request body, so a submitted
    approver name or approved flag cannot influence who a decision is
    attributed to.
    """

    def __init__(self, name: str) -> None:
        self.name = name


def owner_dependency(  # type: ignore[no-untyped-def]
    settings: Settings,
    session_factory: sessionmaker[Session] | None = None,
    throttle: LoginThrottle | None = None,
):
    """Authenticate by bearer token (scripts) or owner session cookie (browser)."""

    guesses = throttle or LoginThrottle()

    def require_owner(request: Request, authorization: str = Header(default="")) -> OwnerIdentity:
        expected = settings.owner_api_token
        if expected is None:
            raise HTTPException(
                status_code=503, detail="Approvals require OWNER_API_TOKEN to be configured"
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            # Constant-time comparison: a token check is an authentication check.
            if secrets.compare_digest(token, expected.get_secret_value()):
                return OwnerIdentity(settings.owner_name)
            # A wrong owner token is a guess like a wrong sign-in.
            peer = peer_of(request)
            slowed = guesses.blocked(peer)
            guesses.fail(peer)
            if slowed:
                raise HTTPException(status_code=429, detail="too_many_attempts")
            raise HTTPException(status_code=401, detail="Owner authentication required")
        raw = request.cookies.get(SESSION_COOKIE, "")
        if raw and session_factory is not None and lookup(session_factory, raw) is not None:
            check_write(request, raw)
            return OwnerIdentity(settings.owner_name)
        raise HTTPException(status_code=401, detail="Owner authentication required")

    return require_owner
