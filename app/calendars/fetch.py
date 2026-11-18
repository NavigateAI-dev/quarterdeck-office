"""Fetching the owner's calendars: a calendar link, or a CalDAV account.

Read straight from the calendar's own server, never through the office's
page reader: a private calendar address is a secret, and the reader is
another company's service. Only web addresses are read, and a calendar is
cut off at MAX_BYTES rather than filling the office's memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"


class CalendarFetchError(Exception):
    """A calendar could not be read; the code says why in a word the page explains."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def web_address(url: str) -> str:
    """The address as https, from the webcal:// form calendars often give."""
    address = url.strip()
    if address.lower().startswith("webcal://"):
        address = "https://" + address[len("webcal://") :]
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise CalendarFetchError("calendar_address_invalid")
    return address


def _check(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise CalendarFetchError("calendar_refused")
    if response.status_code == 404:
        raise CalendarFetchError("calendar_not_found")
    if response.status_code >= 400:
        raise CalendarFetchError("calendar_unavailable")


async def _read(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    content: bytes | None = None,
    auth: tuple[str, str] | None = None,
) -> httpx.Response:
    try:
        async with http.stream(
            method,
            url,
            headers=headers,
            content=content,
            auth=auth,
            timeout=TIMEOUT,
            follow_redirects=True,
        ) as response:
            _check(response)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise CalendarFetchError("calendar_too_large")
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
            )
    except httpx.TimeoutException:
        raise CalendarFetchError("calendar_timed_out") from None
    except httpx.HTTPError:
        raise CalendarFetchError("calendar_unreachable") from None


async def fetch_ics(url: str, http: httpx.AsyncClient) -> str:
    """A calendar link's text."""
    response = await _read(http, "GET", web_address(url), headers={"Accept": "text/calendar"})
    return response.text


async def _dav(
    http: httpx.AsyncClient, method: str, url: str, body: str, depth: str, user: str, password: str
) -> httpx.Response:
    headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
    return await _read(http, method, url, headers, body.encode(), (user, password))


def _responses(text: str) -> list[ElementTree.Element]:
    # A server's reply is outside data: a document declaring entities is
    # refused before parsing, so nothing in it can expand or reach out.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise CalendarFetchError("calendar_server_unreadable")
    try:
        root = ElementTree.fromstring(text)  # noqa: S314 - entities refused above
    except ElementTree.ParseError:
        raise CalendarFetchError("calendar_server_unreadable") from None
    return root.findall(f"{DAV}response")


def _first_href(text: str, prop: str) -> str:
    for response in _responses(text):
        found = response.find(f".//{prop}/{DAV}href")
        if found is not None and found.text:
            return found.text.strip()
    raise CalendarFetchError("calendar_server_unreadable")


PRINCIPAL_BODY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:current-user-principal/></d:prop></d:propfind>"
)
HOME_BODY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)
LIST_BODY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>"
    "</d:prop></d:propfind>"
)


async def caldav_calendars(
    base_url: str, user: str, password: str, http: httpx.AsyncClient
) -> list[dict[str, str]]:
    """The account's calendars that hold events, by address and name."""
    base = web_address(base_url)
    found = await _dav(http, "PROPFIND", base, PRINCIPAL_BODY, "0", user, password)
    principal = urljoin(base, _first_href(found.text, f"{DAV}current-user-principal"))
    found = await _dav(http, "PROPFIND", principal, HOME_BODY, "0", user, password)
    home = urljoin(principal, _first_href(found.text, f"{CALDAV}calendar-home-set"))
    found = await _dav(http, "PROPFIND", home, LIST_BODY, "1", user, password)
    calendars: list[dict[str, str]] = []
    for response in _responses(found.text):
        if response.find(f".//{DAV}resourcetype/{CALDAV}calendar") is None:
            continue
        kinds = [comp.get("name") for comp in response.iterfind(f".//{CALDAV}comp")]
        if kinds and "VEVENT" not in kinds:
            continue
        href = response.findtext(f"{DAV}href") or ""
        name = (response.findtext(f".//{DAV}displayname") or "").strip()
        calendars.append({"url": urljoin(home, href.strip()), "name": name or "Calendar"})
    return calendars


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


async def caldav_events(
    calendar_url: str,
    user: str,
    password: str,
    start: datetime,
    end: datetime,
    http: httpx.AsyncClient,
) -> list[str]:
    """The calendar files of one calendar's events that touch the window."""
    body = (
        '<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-data/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{_stamp(start)}" end="{_stamp(end)}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )
    address = web_address(calendar_url)
    found = await _dav(http, "REPORT", address, body, "1", user, password)
    return [
        data.text
        for response in _responses(found.text)
        for data in response.iterfind(f".//{CALDAV}calendar-data")
        if data.text and data.text.strip()
    ]
