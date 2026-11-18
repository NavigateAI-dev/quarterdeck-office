"""An agent proposes time on the owner's calendar; only a press adds it."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.room_replies import split_calendar

ZONE = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def test_a_proposed_event_is_read_in_the_owners_time_zone() -> None:
    body, event = split_calendar(
        "Thursday morning is free.\nCALENDAR: Draft the report | 2026-10-08 10:00 | 12:00",
        ZONE,
        NOW,
    )
    assert body == "Thursday morning is free."
    assert event == {
        "kind": "add_event",
        "title": "Draft the report",
        "starts_at": "2026-10-08T08:00:00+00:00",
        "ends_at": "2026-10-08T10:00:00+00:00",
    }


def test_a_reply_without_the_line_offers_nothing() -> None:
    assert split_calendar("No time needed.", ZONE, NOW) == ("No time needed.", None)


def test_an_unreadable_or_senseless_time_offers_no_button_and_says_so() -> None:
    for line in (
        "CALENDAR: Draft | tomorrow | later",
        "CALENDAR: Draft | 2026-10-08 12:00 | 2026-10-08 10:00",
        "CALENDAR: Draft | 2026-09-01 10:00 | 2026-09-01 11:00",
        "CALENDAR: Draft | 2026-10-08 10:00 | 2026-11-30 10:00",
        "CALENDAR:  | 2026-10-08 10:00 | 11:00",
    ):
        body, event = split_calendar(f"Sure.\n{line}", ZONE, NOW)
        assert event is None, line
        assert body.startswith("Sure.") and "calendar time could not" in body
