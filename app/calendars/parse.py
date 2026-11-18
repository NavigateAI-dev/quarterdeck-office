"""A calendar file read into events the office can place on the owner's day.

Repeats are expanded within the window asked for, every time is moved to UTC,
and events the owner would not count (cancelled ones) are left out. An event
marked free (TRANSP:TRANSPARENT) is kept but is not busy time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import recurring_ical_events  # type: ignore[import-untyped]
from icalendar import Calendar  # type: ignore[import-untyped]

MAX_TEXT = 4000


class CalendarUnreadable(Exception):
    """The text is not a calendar the office can read."""


@dataclass(frozen=True)
class Event:
    uid: str
    calendar: str
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    busy: bool
    location: str = ""
    description: str = ""
    attendees: int = 0
    calendar_id: str = ""


def _moment(value: Any) -> tuple[datetime, bool]:
    """A start or end as an aware UTC time, and whether it was a whole day."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC), False
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC), True
    raise CalendarUnreadable("event_time_unreadable")


def _text(component: Any, name: str) -> str:
    value = component.get(name)
    return str(value)[:MAX_TEXT].strip() if value is not None else ""


def events_from_ics(text: str, start: datetime, end: datetime, calendar: str = "") -> list[Event]:
    """The events between start and end, repeats expanded, earliest first."""
    try:
        parsed = Calendar.from_ical(text)
    except (ValueError, IndexError, KeyError) as error:
        raise CalendarUnreadable("not_a_calendar") from error
    if parsed.name != "VCALENDAR":
        raise CalendarUnreadable("not_a_calendar")
    name = calendar or _text(parsed, "X-WR-CALNAME")
    try:
        found = recurring_ical_events.of(parsed).between(start, end)
    except Exception as error:  # noqa: BLE001 - the library raises many kinds on bad files
        raise CalendarUnreadable("calendar_repeats_unreadable") from error
    events: list[Event] = []
    for component in found:
        if component.name != "VEVENT":
            continue
        if _text(component, "STATUS").upper() == "CANCELLED":
            continue
        began = component.get("DTSTART")
        if began is None:
            continue
        starts_at, all_day = _moment(began.dt)
        ended = component.get("DTEND")
        if ended is not None:
            ends_at, _ = _moment(ended.dt)
        elif component.get("DURATION") is not None:
            ends_at = starts_at + component.get("DURATION").dt
        else:
            ends_at = starts_at + (timedelta(days=1) if all_day else timedelta(0))
        attendees = component.get("ATTENDEE")
        count = len(attendees) if isinstance(attendees, list) else (1 if attendees else 0)
        uid = _text(component, "UID") or f"{_text(component, 'SUMMARY')}-{starts_at.isoformat()}"
        events.append(
            Event(
                # Each occurrence of a repeat shares the series' UID.
                uid=f"{uid}@{starts_at.isoformat()}",
                calendar=name,
                title=_text(component, "SUMMARY") or "(no title)",
                starts_at=starts_at,
                ends_at=ends_at,
                all_day=all_day,
                busy=_text(component, "TRANSP").upper() != "TRANSPARENT",
                location=_text(component, "LOCATION"),
                description=_text(component, "DESCRIPTION"),
                attendees=count,
            )
        )
    events.sort(key=lambda event: (event.starts_at, event.title))
    return events
