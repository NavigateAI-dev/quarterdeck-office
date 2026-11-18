"""API request and response shapes: calendar."""

from datetime import datetime

from pydantic import Field, model_validator

from app.api.schemas.base import StrictRequest


class CalendarConnect(StrictRequest):
    """One of the owner's calendars: a link, or a CalDAV account.

    The link and the password go to the secret store; neither is returned.
    """

    kind: str = Field(pattern="^(ics|caldav)$")
    provider: str = Field(pattern="^(google|icloud|outlook|fastmail|yahoo|nextcloud|other)$")
    name: str = Field(default="", max_length=255)
    url: str = Field(default="", max_length=4096)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=1024)
    server_url: str = Field(default="", max_length=2048)


class CalendarPreferences(StrictRequest):
    """What the agents may see of the owner's calendars, and how it shapes the work."""

    share: str | None = Field(default=None, pattern="^(busy|titles|all)$")
    hold: bool | None = None
    prep: bool | None = None
    # The owner's time zone, as their browser names it (Europe/Amsterdam).
    timezone: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_+\-/]{1,64}$")


class CalendarPrep(StrictRequest):
    """The meeting's time as the owner reads it, for the brief's wording."""

    when: str = Field(default="", max_length=40)


class MacCalendarEvent(StrictRequest):
    """One event as the helper on the owner's Mac read it from the Calendar app."""

    uid: str = Field(min_length=1, max_length=512)
    calendar: str = Field(default="", max_length=255)
    calendar_id: str = Field(default="", max_length=255)
    title: str = Field(default="(no title)", max_length=4000)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    busy: bool = True
    location: str = Field(default="", max_length=4000)
    description: str = Field(default="", max_length=4000)
    attendees: int = Field(default=0, ge=0, le=10_000)


class MacCalendarReport(StrictRequest):
    """macOS's answer to the helper, and what it read when access was allowed."""

    access: str = Field(pattern="^(granted|denied|restricted|unsupported)$")
    calendars: list["MacCalendarInfo"] = Field(default_factory=list, max_length=200)
    events: list[MacCalendarEvent] = Field(default_factory=list, max_length=20_000)


class CalendarSignInApp(StrictRequest):
    """The owner's own sign-in app at Google (ID and secret) or Microsoft (ID only)."""

    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(default="", max_length=512)


class CalendarSignInStart(StrictRequest):
    """The address the owner's browser uses for the office, for the way back."""

    origin: str = Field(pattern=r"^https?://[A-Za-z0-9.\-\[\]:]+$", max_length=255)


class MacCalendarInfo(StrictRequest):
    """One calendar in the Mac's Calendar app, as macOS describes it."""

    id: str = Field(default="", max_length=255)
    name: str = Field(default="", max_length=255)
    account: str = Field(default="", max_length=255)
    writable: bool = False
    color: str = Field(default="", pattern=r"^(#[0-9a-fA-F]{6})?$")


class CalendarEventEdit(StrictRequest):
    """The owner's own event, made or changed from the Calendar screen."""

    calendar_id: str = Field(default="", max_length=255)
    title: str = Field(min_length=1, max_length=1000)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    location: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def ends_after_it_starts(self) -> "CalendarEventEdit":
        if self.ends_at < self.starts_at:
            raise ValueError("An event ends after it starts")
        return self


class CalendarChangeResult(StrictRequest):
    """How applying one change went, from the Mac helper."""

    ok: bool
    error: str = Field(default="", max_length=64)
