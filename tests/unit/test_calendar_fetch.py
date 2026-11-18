"""Fetching calendars: a link, or a CalDAV account."""

from datetime import UTC, datetime

import httpx
import pytest

from app.calendars.fetch import CalendarFetchError, caldav_calendars, caldav_events, fetch_ics

WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\nBEGIN:VEVENT\r\nUID:a\r\n"
    "SUMMARY:Review\r\nDTSTART:20261007T090000Z\r\nDTEND:20261007T100000Z\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_webcal_link_is_read_over_https() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=ICS)

    async with client(handler) as http:
        text = await fetch_ics("webcal://calendar.example.com/private-abc/basic.ics", http)
    assert seen == ["https://calendar.example.com/private-abc/basic.ics"]
    assert "Review" in text


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "calendar_refused"), (403, "calendar_refused"), (404, "calendar_not_found")],
)
async def test_a_refused_or_missing_link_says_which(status: int, code: str) -> None:
    async with client(lambda request: httpx.Response(status)) as http:
        with pytest.raises(CalendarFetchError) as raised:
            await fetch_ics("https://calendar.example.com/x.ics", http)
    assert raised.value.code == code


async def test_only_web_addresses_are_read() -> None:
    async with client(lambda request: httpx.Response(200, text=ICS)) as http:
        with pytest.raises(CalendarFetchError) as raised:
            await fetch_ics("file:///etc/passwd", http)
    assert raised.value.code == "calendar_address_invalid"


PRINCIPAL = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href>
<d:propstat><d:prop><d:current-user-principal><d:href>/123/principal/</d:href>
</d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>"""
HOME = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/123/principal/</d:href><d:propstat><d:prop><c:calendar-home-set>
<d:href>https://p01-caldav.example.com/123/calendars/</d:href></c:calendar-home-set></d:prop>
</d:propstat></d:response></d:multistatus>"""
LISTING = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/123/calendars/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>
</d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/123/calendars/home/</d:href><d:propstat><d:prop><d:displayname>Home</d:displayname>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set>
<c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
<d:response><d:href>/123/calendars/todo/</d:href><d:propstat><d:prop><d:displayname>Reminders</d:displayname>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set>
<c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
</d:multistatus>"""
REPORT = f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/123/calendars/home/a.ics</d:href><d:propstat><d:prop>
<c:calendar-data>{ICS}</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>"""


def caldav_server(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"].startswith("Basic ")
    body = request.content.decode()
    if request.method == "PROPFIND" and "current-user-principal" in body:
        return httpx.Response(207, text=PRINCIPAL)
    if request.method == "PROPFIND" and "calendar-home-set" in body:
        return httpx.Response(207, text=HOME)
    if request.method == "PROPFIND":
        return httpx.Response(207, text=LISTING)
    if request.method == "REPORT":
        assert "time-range" in body and "20261001T000000Z" in body
        return httpx.Response(207, text=REPORT)
    return httpx.Response(405)


async def test_a_caldav_account_lists_its_event_calendars() -> None:
    async with client(caldav_server) as http:
        found = await caldav_calendars("https://caldav.example.com/", "me@example.com", "pw", http)
    assert found == [{"url": "https://p01-caldav.example.com/123/calendars/home/", "name": "Home"}]


async def test_a_caldav_calendar_gives_its_events_in_the_window() -> None:
    async with client(caldav_server) as http:
        texts = await caldav_events(
            "https://p01-caldav.example.com/123/calendars/home/", "me", "pw", *WINDOW, http
        )
    assert len(texts) == 1 and "Review" in texts[0]


async def test_a_wrong_password_says_so() -> None:
    async with client(lambda request: httpx.Response(401)) as http:
        with pytest.raises(CalendarFetchError) as raised:
            await caldav_calendars("https://caldav.example.com/", "me", "wrong", http)
    assert raised.value.code == "calendar_refused"


async def test_a_reply_declaring_entities_is_refused() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><d:multistatus xmlns:d="DAV:"/>'
    async with client(lambda request: httpx.Response(207, text=bomb)) as http:
        with pytest.raises(CalendarFetchError) as raised:
            await caldav_calendars("https://caldav.example.com/", "me", "pw", http)
    assert raised.value.code == "calendar_server_unreadable"
