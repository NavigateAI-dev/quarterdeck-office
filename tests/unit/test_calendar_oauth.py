"""Signing in to Google or Microsoft for calendars."""

import base64
import hashlib
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.calendars import oauth


def test_the_consent_page_is_asked_with_pkce_and_a_one_time_state() -> None:
    sign_ins = oauth.SignIns()
    url = sign_ins.begin(
        oauth.GOOGLE, "id-123", "http://localhost:8000/calendars/oauth/callback", "back"
    )
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://accounts.google.com/")
    # Reading calendars, and changing events at the owner's word.
    assert query["scope"] == (
        "https://www.googleapis.com/auth/calendar.readonly "
        "https://www.googleapis.com/auth/calendar.events"
    )
    assert query["access_type"] == "offline" and query["code_challenge_method"] == "S256"
    held = sign_ins.take(query["state"])
    digest = hashlib.sha256(held.verifier.encode()).digest()
    assert query["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    with pytest.raises(oauth.SignInError):
        sign_ins.take(query["state"])


def test_a_sign_in_left_ten_minutes_is_forgotten() -> None:
    now = [0.0]
    sign_ins = oauth.SignIns(clock=lambda: now[0])
    url = sign_ins.begin(oauth.MICROSOFT, "id", "http://localhost/cb", "back")
    state = parse_qs(urlparse(url).query)["state"][0]
    now[0] = 601.0
    with pytest.raises(oauth.SignInError) as raised:
        sign_ins.take(state)
    assert raised.value.code == "sign_in_expired"


def test_the_callback_address_names_this_computer_localhost() -> None:
    assert (
        oauth.redirect_for("http://127.0.0.1:8000")
        == "http://localhost:8000/calendars/oauth/callback"
    )


def test_google_events_read_as_the_office_reads_them() -> None:
    timed = oauth.google_event(
        {
            "id": "g1",
            "summary": "Review",
            "start": {"dateTime": "2026-10-07T10:00:00+02:00"},
            "end": {"dateTime": "2026-10-07T11:00:00+02:00"},
            "attendees": [{}, {}],
        },
        "Work",
    )
    assert timed is not None and timed.starts_at == datetime(2026, 10, 7, 8, tzinfo=UTC)
    assert timed.attendees == 2 and timed.busy and timed.calendar == "Work"
    day = oauth.google_event(
        {
            "id": "g2",
            "start": {"date": "2026-10-09"},
            "end": {"date": "2026-10-10"},
            "transparency": "transparent",
        },
        "Work",
    )
    assert day is not None and day.all_day and not day.busy
    assert oauth.google_event({"id": "g3", "status": "cancelled"}, "Work") is None


def test_microsoft_events_read_as_the_office_reads_them() -> None:
    event = oauth.microsoft_event(
        {
            "id": "m1",
            "subject": "1:1",
            "start": {"dateTime": "2026-10-07T08:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-07T08:30:00.0000000", "timeZone": "UTC"},
            "showAs": "free",
            "location": {"displayName": "Room 3"},
        },
        "Calendar",
    )
    assert event is not None and event.starts_at == datetime(2026, 10, 7, 8, tzinfo=UTC)
    assert not event.busy and event.location == "Room 3"
    assert oauth.microsoft_event({"id": "m2", "isCancelled": True}, "C") is None


async def test_an_expired_token_is_refreshed_and_the_refresh_token_kept() -> None:
    seen: list[dict[str, list[str]]] = []

    def token_server(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    held = {"access_token": "old", "refresh_token": "keep-me", "expires_at": time.time() - 5}
    app = {"client_id": "id", "client_secret": "s"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(token_server)) as http:
        token, renewed = await oauth.access_token(oauth.GOOGLE, app, held, http)
    assert token == "new" and renewed is not None and renewed["refresh_token"] == "keep-me"
    assert seen[0]["grant_type"] == ["refresh_token"] and seen[0]["client_secret"] == ["s"]


async def test_a_refused_refresh_says_sign_in_again() -> None:
    def token_server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "x"})

    held = {"refresh_token": "gone", "expires_at": 0}
    async with httpx.AsyncClient(transport=httpx.MockTransport(token_server)) as http:
        with pytest.raises(oauth.SignInError) as raised:
            await oauth.access_token(oauth.MICROSOFT, {"client_id": "id"}, held, http)
    assert raised.value.code == "sign_in_refused"


def test_an_all_day_event_is_sent_as_dates_in_the_owners_zone() -> None:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Amsterdam")
    wanted = {
        "title": "Off",
        "all_day": True,
        "starts_at": "2026-10-08T22:00:00+00:00",
        "ends_at": "2026-10-09T22:00:00+00:00",
    }
    google = oauth.google_body(wanted, zone)
    assert google["start"] == {"date": "2026-10-09"} and google["end"] == {"date": "2026-10-10"}
    microsoft = oauth.microsoft_body(wanted, zone)
    assert microsoft["isAllDay"] is True
    assert microsoft["start"] == {"dateTime": "2026-10-09T00:00:00", "timeZone": "Europe/Amsterdam"}
    timed = oauth.google_body(
        {"title": "Call", "starts_at": "2026-10-08T08:00:00Z", "ends_at": "2026-10-08T09:00:00Z"},
        zone,
    )
    assert timed["start"]["dateTime"].startswith("2026-10-08T10:00:00")


def test_consent_to_change_is_read_from_the_granted_scope() -> None:
    assert oauth.may_change(
        oauth.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events"
        },
    )
    assert not oauth.may_change(
        oauth.GOOGLE, {"scope": "https://www.googleapis.com/auth/calendar.readonly"}
    )
    assert oauth.may_change(oauth.MICROSOFT, {"scope": "Calendars.ReadWrite User.Read"})
    assert not oauth.may_change(oauth.MICROSOFT, {})


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (httpx.ConnectError("down"), "calendar_unreachable"),
        (httpx.ReadTimeout("slow"), oauth.NO_ANSWER),
    ],
)
async def test_a_change_not_sent_failed_and_one_sent_unanswered_is_unknown(
    raised: Exception, code: str
) -> None:
    """Stage 5c: not reaching the calendar means nothing changed; sending and
    hearing nothing back means it may have, and is not sent again."""

    def calendar(request: httpx.Request) -> httpx.Response:
        raise raised

    wanted = {
        "calendar_id": "primary",
        "title": "Call",
        "starts_at": "2026-10-20T09:00:00+00:00",
        "ends_at": "2026-10-20T09:30:00+00:00",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(calendar)) as http:
        with pytest.raises(oauth.SignInError) as failed:
            await oauth.change_event(oauth.GOOGLE, "token", "delete", wanted, ZoneInfo("UTC"), http)
    assert failed.value.code == code
