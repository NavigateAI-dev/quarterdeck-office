"""Timezone-aware schedule arithmetic."""

from datetime import UTC, datetime, timedelta

import pytest

from app.scheduling import Recurrence, next_occurrence, preview

AMS = "Europe/Amsterdam"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_a_daily_schedule_runs_at_local_time() -> None:
    rule = Recurrence(timezone=AMS, frequency="daily", local_time="09:00")
    # 19 Sep 2026: Amsterdam is UTC+2, so 09:00 local is 07:00 UTC.
    assert next_occurrence(rule, utc(2026, 9, 19, 6, 0)) == utc(2026, 9, 19, 7, 0)
    assert next_occurrence(rule, utc(2026, 9, 19, 7, 0)) == utc(2026, 9, 20, 7, 0)


def test_winter_time_shifts_the_utc_hour_not_the_local_hour() -> None:
    rule = Recurrence(timezone=AMS, frequency="daily", local_time="09:00")
    assert next_occurrence(rule, utc(2026, 11, 2, 0, 0)) == utc(2026, 11, 2, 8, 0)


def test_a_local_time_skipped_by_spring_forward_moves_forward() -> None:
    """29 Mar 2026: 02:00-03:00 does not exist in Amsterdam."""
    rule = Recurrence(timezone=AMS, frequency="daily", local_time="02:30")
    assert next_occurrence(rule, utc(2026, 3, 28, 12, 0)) == utc(2026, 3, 29, 1, 30)  # 03:30 CEST


def test_a_repeated_local_time_on_fall_back_runs_once() -> None:
    """25 Oct 2026: 02:00-03:00 happens twice; run on the first, not both."""
    rule = Recurrence(timezone=AMS, frequency="daily", local_time="02:30")
    first = next_occurrence(rule, utc(2026, 10, 24, 12, 0))
    assert first == utc(2026, 10, 25, 0, 30)  # 02:30 CEST
    assert next_occurrence(rule, first) == utc(2026, 10, 26, 1, 30)  # next day, CET


def test_a_weekly_schedule_runs_on_its_weekday() -> None:
    rule = Recurrence(timezone=AMS, frequency="weekly", local_time="08:00", weekday=0)  # Monday
    # Saturday 19 Sep 2026 -> Monday 21 Sep.
    assert next_occurrence(rule, utc(2026, 9, 19, 12, 0)) == utc(2026, 9, 21, 6, 0)


def test_missed_occurrences_are_skipped_not_caught_up() -> None:
    rule = Recurrence(timezone=AMS, frequency="daily", local_time="09:00")
    # The office was asleep for three days; the next run is the next future one only.
    assert next_occurrence(rule, utc(2026, 9, 23, 12, 0)) == utc(2026, 9, 24, 7, 0)


def test_the_preview_lists_the_next_runs_in_local_time() -> None:
    rule = Recurrence(timezone=AMS, frequency="weekly", local_time="08:00", weekday=4)
    assert preview(rule, utc(2026, 10, 20, 0, 0), 2) == [
        "2026-10-23 08:00 CEST",
        "2026-10-30 08:00 CET",
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"timezone": "Mars/Olympus", "frequency": "daily", "local_time": "09:00"},
        {"timezone": AMS, "frequency": "hourly", "local_time": "09:00"},
        {"timezone": AMS, "frequency": "daily", "local_time": "25:00"},
        {"timezone": AMS, "frequency": "weekly", "local_time": "09:00"},
    ],
)
def test_invalid_rules_are_refused(fields: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Recurrence(**fields)


def test_cron_runs_on_weekdays_at_the_set_minute() -> None:
    rule = Recurrence(timezone="UTC", frequency="cron", local_time="00:00", cron="30 9 * * 1-5")
    saturday = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert next_occurrence(rule, saturday) == datetime(2026, 11, 9, 9, 30, tzinfo=UTC)


def test_a_bad_cron_expression_is_refused() -> None:
    with pytest.raises(ValueError):
        Recurrence(timezone="UTC", frequency="cron", local_time="00:00", cron="99 * * * *")


def test_a_one_time_job_has_no_next_time_once_passed() -> None:
    at = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    rule = Recurrence(timezone="UTC", frequency="once", local_time="00:00", once_at=at)
    assert next_occurrence(rule, at - timedelta(minutes=1)) == at
    assert next_occurrence(rule, at) is None


def test_every_few_hours_counts_from_now() -> None:
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    rule = Recurrence(timezone="UTC", frequency="hours", local_time="00:00", every_hours=4)
    assert next_occurrence(rule, now) == now + timedelta(hours=4)
