"""Timezone-aware recurrence for scheduled tasks.

Rules are expressed in the owner's local time. A local time that does not
exist on a spring-forward day moves forward by the gap; a local time that
occurs twice on a fall-back day runs once, at its first occurrence. Missed
occurrences are skipped: the next run is always the next one in the future.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Recurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = Field(min_length=1, max_length=64)
    frequency: str = Field(pattern="^(daily|weekly|hours|cron|once)$")
    local_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # Monday = 0 ... Sunday = 6; required for weekly rules.
    weekday: int | None = Field(default=None, ge=0, le=6)
    every_hours: int | None = Field(default=None, ge=1, le=168)
    cron: str | None = Field(default=None, max_length=64)
    once_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        return value

    @model_validator(mode="after")
    def weekly_needs_a_weekday(self) -> "Recurrence":
        if self.frequency == "weekly" and self.weekday is None:
            raise ValueError("A weekly schedule needs a weekday")
        if self.frequency == "hours" and self.every_hours is None:
            raise ValueError("It needs a number of hours")
        if self.frequency == "once" and (self.once_at is None or self.once_at.tzinfo is None):
            raise ValueError("It needs a time with its zone")
        if self.frequency == "cron":
            _cron_fields(self.cron or "")
        return self


def _at(day: date, wall: time, zone: ZoneInfo) -> datetime:
    """The UTC instant of a local wall-clock time on a day, DST-resolved."""
    local = datetime.combine(day, wall, tzinfo=zone).replace(fold=0)
    # A nonexistent time does not survive a round trip through UTC; step past
    # the gap by the offset change so 02:30 on a spring-forward day is 03:30.
    roundtrip = local.astimezone(UTC).astimezone(zone)
    if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
        gap = (local + timedelta(hours=3)).utcoffset() - (local - timedelta(hours=3)).utcoffset()  # type: ignore[operator]
        local = (local.replace(tzinfo=None) + gap).replace(tzinfo=zone)
    return local.astimezone(UTC)


def _cron_field(text: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        span, _, step = part.partition("/")
        if span == "*":
            first, last = low, high
        else:
            first_text, _, last_text = span.partition("-")
            first = int(first_text)
            last = int(last_text) if last_text else (high if step else first)
        if first < low or last > high or first > last or (step and int(step) < 1):
            raise ValueError("That cron expression is not valid")
        values.update(range(first, last + 1, int(step) if step else 1))
    return values


def _cron_fields(expression: str) -> list[set[int]]:
    """Minute, hour, day of month, month and day of week (Sunday 0 or 7)."""
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("A cron expression has five fields")
    try:
        return [
            _cron_field(parts[0], 0, 59),
            _cron_field(parts[1], 0, 23),
            _cron_field(parts[2], 1, 31),
            _cron_field(parts[3], 1, 12),
            {day % 7 for day in _cron_field(parts[4], 0, 7)},
        ]
    except ValueError:
        raise ValueError("That cron expression is not valid") from None


def _next_cron(expression: str, zone: ZoneInfo, after: datetime) -> datetime | None:
    minutes, hours, days, months, weekdays = _cron_fields(expression)
    # Either day field matches when both are set, as in standard cron.
    any_day = len(days) == 31
    any_weekday = len(weekdays) == 7
    day = after.astimezone(zone).date() - timedelta(days=1)
    for _ in range(800):
        cron_weekday = (day.weekday() + 1) % 7
        by_date = day.day in days
        by_weekday = cron_weekday in weekdays
        matched = (
            (by_date or by_weekday) if not (any_day or any_weekday) else by_date and by_weekday
        )
        if day.month in months and matched:
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    candidate = _at(day, time(hour, minute), zone)
                    if candidate > after:
                        return candidate
        day += timedelta(days=1)
    return None


def next_occurrence(rule: Recurrence, after: datetime) -> datetime | None:
    """The first scheduled instant strictly after `after` (UTC); None when there
    is no more, as with a one-time job that has passed."""
    zone = ZoneInfo(rule.timezone)
    if rule.frequency == "once":
        return rule.once_at if rule.once_at and rule.once_at > after else None
    if rule.frequency == "hours":
        return after + timedelta(hours=rule.every_hours or 1)
    if rule.frequency == "cron":
        return _next_cron(rule.cron or "", zone, after)
    hour, minute = map(int, rule.local_time.split(":"))
    wall = time(hour, minute)
    day = after.astimezone(zone).date() - timedelta(days=1)
    for _ in range(16):
        if rule.frequency == "daily" or day.weekday() == rule.weekday:
            candidate = _at(day, wall, zone)
            if candidate > after:
                return candidate
        day += timedelta(days=1)
    raise AssertionError("No occurrence within two weeks")  # pragma: no cover


def preview(rule: Recurrence, after: datetime, count: int = 3) -> list[str]:
    zone = ZoneInfo(rule.timezone)
    moments: list[str] = []
    current = after
    for _ in range(count):
        upcoming = next_occurrence(rule, current)
        if upcoming is None:
            break
        current = upcoming
        moments.append(current.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z"))
    return moments
