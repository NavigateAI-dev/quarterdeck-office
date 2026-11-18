"""Signing in to Google or Microsoft for the owner's calendars.

The owner's own consent, on the company's own page: the office never sees a
password. Each company shows that page only for an app registered with it, so
the owner registers one for their office once (a Google sign-in, or a
Microsoft app registration) and pastes its ID here; after that it is one
click. The flow is OAuth 2.0 with PKCE and a single-use state; the
tokens are sealed in the secret store and refreshed before they expire. Read
only: Google's calendar.readonly scope, Microsoft's Calendars.Read.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as day_time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.calendars.parse import Event

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
STATE_LIFETIME = 600.0
MAX_PAGES = 20


@dataclass(frozen=True)
class Provider:
    key: str
    kind: str
    name: str
    authorize: str
    token: str
    scope: str
    extra: dict[str, str]
    needs_secret: bool


GOOGLE = Provider(
    key="google",
    kind="google_api",
    name="Google",
    authorize="https://accounts.google.com/o/oauth2/v2/auth",
    token="https://oauth2.googleapis.com/token",  # noqa: S106 - an address, not a secret
    # Reading the calendar list, and the owner's own changes to events.
    scope="https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/calendar.events",
    # offline: a refresh token, so the owner signs in once, not every hour.
    extra={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
    needs_secret=True,
)
MICROSOFT = Provider(
    key="microsoft",
    kind="microsoft_api",
    name="Microsoft",
    authorize="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
    scope="offline_access Calendars.ReadWrite User.Read",
    extra={"prompt": "select_account"},
    needs_secret=False,
)
PROVIDERS = {GOOGLE.key: GOOGLE, MICROSOFT.key: MICROSOFT}
BY_KIND = {GOOGLE.kind: GOOGLE, MICROSOFT.kind: MICROSOFT}


class SignInError(Exception):
    """The sign-in or a calendar read failed; the code says why."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class Pending:
    provider: str
    verifier: str
    redirect_uri: str
    back_to: str
    created: float


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class SignIns:
    """Sign-ins under way: each state works once, for ten minutes."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self.pending: dict[str, Pending] = {}
        self.clock = clock

    def begin(self, provider: Provider, client_id: str, redirect_uri: str, back_to: str) -> str:
        """The address of the company's consent page for this sign-in."""
        self._forget_old()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self.pending[state] = Pending(provider.key, verifier, redirect_uri, back_to, self.clock())
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": provider.scope,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            **provider.extra,
        }
        return f"{provider.authorize}?{urlencode(query, quote_via=quote)}"

    def take(self, state: str) -> Pending:
        self._forget_old()
        held = self.pending.pop(state, None)
        if held is None:
            raise SignInError("sign_in_expired")
        return held

    def _forget_old(self) -> None:
        now = self.clock()
        for state in [s for s, p in self.pending.items() if now - p.created > STATE_LIFETIME]:
            self.pending.pop(state, None)


async def _token_request(
    provider: Provider, app: dict[str, str], form: dict[str, str], http: httpx.AsyncClient
) -> dict[str, Any]:
    body = {"client_id": app["client_id"], **form}
    if provider.needs_secret:
        body["client_secret"] = app.get("client_secret", "")
    try:
        response = await http.post(provider.token, data=body, timeout=TIMEOUT)
    except httpx.HTTPError:
        raise SignInError("sign_in_unreachable") from None
    if response.status_code >= 400:
        # The company's own error code only; its text is never carried.
        try:
            code = str(response.json().get("error") or "")
        except ValueError:
            code = ""
        raise SignInError(
            "sign_in_refused"
            if code in ("invalid_grant", "invalid_client", "unauthorized_client")
            else "sign_in_failed"
        )
    tokens = dict(response.json())
    if not tokens.get("access_token"):
        raise SignInError("sign_in_failed")
    return tokens


def _held(tokens: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """What is sealed: the refresh token (kept when not reissued) and when to refresh."""
    held = dict(previous or {})
    held["access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        held["refresh_token"] = tokens["refresh_token"]
    held["expires_at"] = time.time() + float(tokens.get("expires_in") or 3600) - 120
    if tokens.get("scope"):
        held["scope"] = str(tokens["scope"])
    return held


def may_change(provider: Provider, held: dict[str, Any]) -> bool:
    """Whether the owner's consent covered changing events, not only reading."""
    granted = str(held.get("scope") or "").lower()
    if provider is GOOGLE:
        return "auth/calendar.events" in granted or granted.endswith("auth/calendar")
    return "calendars.readwrite" in granted


async def finish(
    provider: Provider,
    app: dict[str, str],
    code: str,
    pending: Pending,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    tokens = await _token_request(
        provider,
        app,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.verifier,
        },
        http,
    )
    if not tokens.get("refresh_token"):
        # Without it the owner would be asked again within the hour.
        raise SignInError("sign_in_no_refresh")
    return _held(tokens)


async def access_token(
    provider: Provider, app: dict[str, str], held: dict[str, Any], http: httpx.AsyncClient
) -> tuple[str, dict[str, Any] | None]:
    """A usable access token, and the new sealed tokens when they were refreshed."""
    if held.get("access_token") and float(held.get("expires_at") or 0) > time.time():
        return str(held["access_token"]), None
    if not held.get("refresh_token"):
        raise SignInError("sign_in_refused")
    tokens = await _token_request(
        provider,
        app,
        {"grant_type": "refresh_token", "refresh_token": str(held["refresh_token"])},
        http,
    )
    renewed = _held(tokens, held)
    return str(renewed["access_token"]), renewed


async def _get(http: httpx.AsyncClient, url: str, token: str, **headers: str) -> dict[str, Any]:
    try:
        response = await http.get(
            url, headers={"Authorization": f"Bearer {token}", **headers}, timeout=TIMEOUT
        )
    except httpx.HTTPError:
        raise SignInError("calendar_unreachable") from None
    if response.status_code in (401, 403):
        raise SignInError("calendar_refused")
    if response.status_code >= 400:
        raise SignInError("calendar_unavailable")
    return dict(response.json())


# ---- Google Calendar ----

GOOGLE_API = "https://www.googleapis.com/calendar/v3"


def _google_time(value: dict[str, Any]) -> tuple[datetime, bool]:
    if value.get("dateTime"):
        moment = datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
        return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC), False
    day = date.fromisoformat(str(value.get("date")))
    return datetime.combine(day, day_time.min, tzinfo=UTC), True


def google_event(item: dict[str, Any], calendar: str, calendar_id: str = "") -> Event | None:
    if item.get("status") == "cancelled" or not item.get("start"):
        return None
    starts_at, all_day = _google_time(item["start"])
    ends_at, _ = _google_time(item.get("end") or item["start"])
    return Event(
        uid=f"{item.get('id', '')}@{starts_at.isoformat()}"[:512],
        calendar=calendar[:255],
        calendar_id=calendar_id[:255],
        title=str(item.get("summary") or "(no title)")[:4000],
        starts_at=starts_at,
        ends_at=max(ends_at, starts_at),
        all_day=all_day,
        busy=item.get("transparency") != "transparent",
        location=str(item.get("location") or "")[:4000],
        description=str(item.get("description") or "")[:4000],
        attendees=len(item.get("attendees") or []),
    )


async def google_account(token: str, http: httpx.AsyncClient) -> tuple[str, list[dict[str, Any]]]:
    """The account's address and its calendars that are shown in Google Calendar."""
    listing = await _get(
        http, f"{GOOGLE_API}/users/me/calendarList?minAccessRole=freeBusyReader", token
    )
    calendars = [
        {
            "id": str(c["id"]),
            "name": str(c.get("summaryOverride") or c.get("summary") or c["id"]),
            "writable": c.get("accessRole") in ("owner", "writer"),
            "color": str(c.get("backgroundColor") or "")[:7],
        }
        for c in listing.get("items", [])
        if c.get("selected", True) and not c.get("hidden")
    ]
    primary = next((str(c["id"]) for c in listing.get("items", []) if c.get("primary")), "")
    return primary, calendars


async def google_events(
    token: str,
    calendars: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    http: httpx.AsyncClient,
) -> list[Event]:
    found: list[Event] = []
    for calendar in calendars:
        page = ""
        for _ in range(MAX_PAGES):
            query = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "maxResults": "2500",
                **({"pageToken": page} if page else {}),
            }
            url = (
                f"{GOOGLE_API}/calendars/{quote(calendar['id'], safe='')}/events?{urlencode(query)}"
            )
            reply = await _get(http, url, token)
            found.extend(
                e
                for e in (
                    google_event(i, calendar["name"], calendar["id"])
                    for i in reply.get("items", [])
                )
                if e
            )
            page = str(reply.get("nextPageToken") or "")
            if not page:
                break
    return found


# ---- Microsoft Outlook and 365 ----

GRAPH = "https://graph.microsoft.com/v1.0"


def _graph_time(value: dict[str, Any]) -> datetime:
    # Asked for in UTC (the Prefer header), without an offset; seven decimals.
    text = str(value.get("dateTime") or "")[:26]
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def microsoft_event(item: dict[str, Any], calendar: str, calendar_id: str = "") -> Event | None:
    if item.get("isCancelled") or not item.get("start"):
        return None
    starts_at = _graph_time(item["start"])
    ends_at = _graph_time(item.get("end") or item["start"])
    return Event(
        uid=f"{item.get('id', '')}"[:480] + f"@{starts_at.isoformat()}",
        calendar=calendar[:255],
        calendar_id=calendar_id[:255],
        title=str(item.get("subject") or "(no title)")[:4000],
        starts_at=starts_at,
        ends_at=max(ends_at, starts_at),
        all_day=bool(item.get("isAllDay")),
        busy=item.get("showAs") not in ("free", "workingElsewhere"),
        location=str((item.get("location") or {}).get("displayName") or "")[:4000],
        description=str(item.get("bodyPreview") or "")[:4000],
        attendees=len(item.get("attendees") or []),
    )


async def microsoft_account(
    token: str, http: httpx.AsyncClient
) -> tuple[str, list[dict[str, Any]]]:
    me = await _get(http, f"{GRAPH}/me?$select=mail,userPrincipalName", token)
    listing = await _get(
        http, f"{GRAPH}/me/calendars?$select=id,name,canEdit,hexColor&$top=100", token
    )
    calendars = [
        {
            "id": str(c["id"]),
            "name": str(c.get("name") or "Calendar"),
            "writable": bool(c.get("canEdit")),
            "color": str(c.get("hexColor") or "")[:7],
        }
        for c in listing.get("value", [])
    ]
    return str(me.get("mail") or me.get("userPrincipalName") or ""), calendars


async def microsoft_events(
    token: str,
    calendars: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    http: httpx.AsyncClient,
) -> list[Event]:
    found: list[Event] = []
    select = "id,subject,start,end,isAllDay,showAs,location,bodyPreview,attendees,isCancelled"
    for calendar in calendars:
        query = urlencode(
            {
                "startDateTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "$top": "500",
                "$select": select,
            }
        )
        url = f"{GRAPH}/me/calendars/{quote(calendar['id'], safe='')}/calendarView?{query}"
        for _ in range(MAX_PAGES):
            reply = await _get(http, url, token, Prefer='outlook.timezone="UTC"')
            found.extend(
                e
                for e in (
                    microsoft_event(i, calendar["name"], calendar["id"])
                    for i in reply.get("value", [])
                )
                if e
            )
            url = str(reply.get("@odata.nextLink") or "")
            if not url.startswith(GRAPH):
                break
    return found


async def account(
    provider: Provider, token: str, http: httpx.AsyncClient
) -> tuple[str, list[dict[str, Any]]]:
    if provider is GOOGLE:
        return await google_account(token, http)
    return await microsoft_account(token, http)


async def events(
    provider: Provider,
    token: str,
    calendars: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    http: httpx.AsyncClient,
) -> list[Event]:
    if provider is GOOGLE:
        return await google_events(token, calendars, start, end, http)
    return await microsoft_events(token, calendars, start, end, http)


def redirect_for(origin: str) -> str:
    """The callback address both companies accept for an office on this computer.

    Both take http only for "localhost", so 127.0.0.1 is named localhost; the
    owner registers exactly the address the page shows them.
    """
    base = origin.rstrip("/").replace("://127.0.0.1", "://localhost")
    return f"{base}/calendars/oauth/callback"


# ---- the owner's changes ----


def event_id_of(uid: str) -> str:
    """The company's own event id; the office's uid adds the occurrence's start."""
    return uid.rpartition("@")[0] or uid


# A change sent with no answer back: made or not, nobody knows yet.
NO_ANSWER = "calendar_no_answer"


async def _send(
    http: httpx.AsyncClient, method: str, url: str, token: str, body: Any = None
) -> dict[str, Any]:
    try:
        response = await http.request(
            method,
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise SignInError("calendar_unreachable") from None
    except httpx.HTTPError:
        # Sent, and no answer came back: whether it changed is not known (5c).
        raise SignInError(NO_ANSWER) from None
    if response.status_code in (401, 403):
        raise SignInError("calendar_refused")
    if response.status_code in (404, 410):
        raise SignInError("event_not_found")
    if response.status_code >= 400:
        raise SignInError("calendar_change_failed")
    try:
        return dict(response.json()) if response.content else {}
    except ValueError:
        return {}


def _local(value: str, zone: Any) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone)


def google_body(wanted: dict[str, Any], zone: Any) -> dict[str, Any]:
    starts, ends = _local(wanted["starts_at"], zone), _local(wanted["ends_at"], zone)
    if wanted.get("all_day"):
        start: dict[str, str] = {"date": starts.date().isoformat()}
        end: dict[str, str] = {"date": max(ends.date(), starts.date()).isoformat()}
        if end["date"] == start["date"]:
            end = {"date": date.fromordinal(starts.date().toordinal() + 1).isoformat()}
    else:
        start = {"dateTime": starts.isoformat(), "timeZone": zone.key}
        end = {"dateTime": ends.isoformat(), "timeZone": zone.key}
    return {
        "summary": wanted.get("title") or "(no title)",
        "location": wanted.get("location") or "",
        "description": wanted.get("notes") or "",
        "start": start,
        "end": end,
    }


def microsoft_body(wanted: dict[str, Any], zone: Any) -> dict[str, Any]:
    starts, ends = _local(wanted["starts_at"], zone), _local(wanted["ends_at"], zone)
    if wanted.get("all_day"):
        starts = starts.replace(hour=0, minute=0, second=0, microsecond=0)
        ends = ends.replace(hour=0, minute=0, second=0, microsecond=0)
        if ends <= starts:
            ends = datetime.fromordinal(starts.toordinal() + 1).replace(tzinfo=zone)
    stamp = "%Y-%m-%dT%H:%M:%S"
    return {
        "subject": wanted.get("title") or "(no title)",
        "body": {"contentType": "text", "content": wanted.get("notes") or ""},
        "location": {"displayName": wanted.get("location") or ""},
        "isAllDay": bool(wanted.get("all_day")),
        "start": {"dateTime": starts.strftime(stamp), "timeZone": zone.key},
        "end": {"dateTime": ends.strftime(stamp), "timeZone": zone.key},
    }


async def change_event(
    provider: Provider,
    token: str,
    action: str,
    wanted: dict[str, Any],
    zone: Any,
    http: httpx.AsyncClient,
) -> None:
    """Make the owner's change in their Google or Microsoft calendar.

    One occurrence of a repeat is its own event to both companies (the office
    reads them expanded), so only the occurrence chosen changes. Moving an
    event to another calendar is made as a new event there and the old one
    removed, which both companies support alike.
    """
    calendar = str(wanted.get("calendar_id") or "")
    source = str(wanted.get("from_id") or calendar)
    event = event_id_of(str(wanted.get("uid") or ""))
    google = provider is GOOGLE

    def in_calendar(cid: str) -> str:
        if google:
            return f"{GOOGLE_API}/calendars/{quote(cid, safe='')}/events"
        return f"{GRAPH}/me/calendars/{quote(cid, safe='')}/events"

    def one(cid: str) -> str:
        if google:
            return f"{in_calendar(cid)}/{quote(event, safe='')}"
        return f"{GRAPH}/me/events/{quote(event, safe='')}"

    body = google_body(wanted, zone) if google and action != "delete" else None
    if not google and action != "delete":
        body = microsoft_body(wanted, zone)
    if action == "create":
        await _send(http, "POST", in_calendar(calendar), token, body)
    elif action == "delete":
        await _send(http, "DELETE", one(source), token)
    elif calendar != source:
        await _send(http, "POST", in_calendar(calendar), token, body)
        await _send(http, "DELETE", one(source), token)
    else:
        await _send(http, "PATCH", one(source), token, body)
