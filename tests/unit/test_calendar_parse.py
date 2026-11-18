"""Reading a calendar file into the owner's day."""

from datetime import UTC, datetime, timedelta

from app.calendars.parse import events_from_ics

WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
X-WR-CALNAME:Work
BEGIN:VTIMEZONE
TZID:Europe/Amsterdam
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:call-1
SUMMARY:Client call
DESCRIPTION:Dial-in 123
LOCATION:Zoom
DTSTART;TZID=Europe/Amsterdam:20261007T100000
DTEND;TZID=Europe/Amsterdam:20261007T110000
ATTENDEE:mailto:a@example.com
ATTENDEE:mailto:b@example.com
END:VEVENT
BEGIN:VEVENT
UID:standup
SUMMARY:Stand-up
DTSTART:20261005T080000Z
DTEND:20261005T081500Z
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:Day off
DTSTART;VALUE=DATE:20261009
DTEND;VALUE=DATE:20261010
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261008T120000Z
DTEND:20261008T130000Z
END:VEVENT
BEGIN:VEVENT
UID:old
SUMMARY:Long ago
DTSTART:20250101T090000Z
DTEND:20250101T100000Z
END:VEVENT
END:VCALENDAR
"""


def by_title(title: str) -> list:
    return [event for event in events_from_ics(ICS, *WINDOW) if event.title == title]


def test_a_timed_event_keeps_its_zone_and_details() -> None:
    [call] = by_title("Client call")
    assert call.starts_at == datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    assert call.ends_at - call.starts_at == timedelta(hours=1)
    assert call.location == "Zoom" and call.description == "Dial-in 123"
    assert call.attendees == 2 and call.busy and not call.all_day
    assert call.calendar == "Work"


def test_a_repeating_event_becomes_each_of_its_days() -> None:
    standups = by_title("Stand-up")
    assert len(standups) == 5
    assert len({event.uid for event in standups}) == 5, "each occurrence is told apart"


def test_an_all_day_free_event_is_not_busy() -> None:
    [day_off] = by_title("Day off")
    assert day_off.all_day and not day_off.busy


def test_cancelled_and_out_of_window_events_are_left_out() -> None:
    assert by_title("Cancelled thing") == []
    assert by_title("Long ago") == []


def test_a_broken_file_says_so() -> None:
    import pytest

    from app.calendars.parse import CalendarUnreadable

    with pytest.raises(CalendarUnreadable):
        events_from_ics("<html>not a calendar</html>", *WINDOW)
