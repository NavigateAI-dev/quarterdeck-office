"""The helper on the owner's Mac: asked only after the press."""

import plistlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import mac_calendar as helper


class Stamp:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def timeIntervalSince1970(self) -> float:  # noqa: N802 - EventKit's name
        return self.seconds


class Named:
    def __init__(self, title: str, source: Any = None, writable: bool = True) -> None:
        self._title, self._source, self._writable = title, source, writable

    def title(self) -> str:
        return self._title

    def source(self) -> Any:
        return self._source

    def calendarIdentifier(self) -> str:  # noqa: N802
        return f"id-{self._title}"

    def allowsContentModifications(self) -> bool:  # noqa: N802
        return self._writable


class FakeEvent:
    def __init__(self, **values: Any) -> None:
        self.values = {
            "status": 1,
            "start": datetime(2026, 10, 7, 8, tzinfo=UTC).timestamp(),
            "end": datetime(2026, 10, 7, 9, tzinfo=UTC).timestamp(),
            "identifier": "ABC",
            "calendar": Named("Home"),
            "title": "Dentist",
            "all_day": False,
            "availability": 0,
            "location": "Main St",
            "notes": "Bring card",
            "attendees": None,
        } | values

    def status(self) -> int:
        return self.values["status"]

    def startDate(self) -> Stamp:  # noqa: N802
        return Stamp(self.values["start"])

    def endDate(self) -> Stamp:  # noqa: N802
        return Stamp(self.values["end"])

    def eventIdentifier(self) -> str:  # noqa: N802
        return self.values["identifier"]

    def calendarItemIdentifier(self) -> str:  # noqa: N802
        return "ITEM"

    def calendar(self) -> Any:
        return self.values["calendar"]

    def title(self) -> str:
        return self.values["title"]

    def isAllDay(self) -> bool:  # noqa: N802
        return self.values["all_day"]

    def availability(self) -> int:
        return self.values["availability"]

    def location(self) -> str:
        return self.values["location"]

    def notes(self) -> str:
        return self.values["notes"]

    def attendees(self) -> Any:
        return self.values["attendees"]


def test_an_event_from_the_calendar_app_is_sent_as_the_office_reads_it() -> None:
    sent = helper.event_payload(FakeEvent(attendees=["a", "b"]))
    assert sent is not None
    assert sent["title"] == "Dentist" and sent["calendar"] == "Home"
    assert sent["starts_at"] == "2026-10-07T08:00:00+00:00" and sent["busy"] is True
    assert sent["attendees"] == 2 and sent["uid"].startswith("ABC@")
    assert sent["calendar_id"] == "id-Home"


def test_free_and_cancelled_events_are_told_apart() -> None:
    free = helper.event_payload(FakeEvent(availability=helper.FREE))
    assert free is not None and free["busy"] is False
    assert helper.event_payload(FakeEvent(status=helper.CANCELED)) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="The helper app is signed with macOS codesign")
def test_the_helper_app_names_itself_and_says_why_it_asks(tmp_path: Path) -> None:
    app = tmp_path / "Quarterdeck Calendar.app"
    assert helper.install(app, tmp_path) is True
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleName"] == "Quarterdeck Calendar" and info["LSUIElement"] is True
    assert "Read only" in info["NSCalendarsFullAccessUsageDescription"]
    script = (app / "Contents" / "MacOS" / "helper").read_text()
    assert "-m scripts.mac_calendar run" in script
    assert helper.install(app, tmp_path) is False, "unchanged, so macOS keeps the answer"


class Calls:
    def __init__(self, status: str = helper.UNKNOWN, answer: str = helper.GRANTED) -> None:
        self.state, self.answer = status, answer
        self.asked = 0
        self.reports: list[dict[str, Any]] = []

    def status(self) -> str:
        return self.state

    def ask(self) -> str:
        self.asked += 1
        return self.answer

    def read(self) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        return [{"name": "Home", "account": "iCloud"}], [{"uid": "x"}]

    def report(self, body: dict[str, Any]) -> None:
        self.reports.append(body)

    def edits(self) -> int:
        self.edited = getattr(self, "edited", 0) + 1
        return 1

    def tick(self, poll: dict[str, bool], last: float | None, now: float = 1000.0) -> Any:
        return helper.tick(
            poll, self.status, self.ask, self.read, self.report, last, now, self.edits
        )


WANTED = {"wanted": True, "ask": False, "sync": False}


def test_nothing_is_asked_or_read_until_the_owner_presses() -> None:
    calls = Calls()
    assert calls.tick({"wanted": False, "ask": False, "sync": False}, None) is None
    assert calls.tick(WANTED, None) is None
    assert calls.asked == 0 and calls.reports == []


def test_the_press_asks_macos_and_sends_the_events_when_allowed() -> None:
    calls = Calls()
    assert calls.tick(WANTED | {"ask": True}, None) == 1000.0
    assert calls.asked == 1
    assert calls.reports[0]["access"] == "granted" and calls.reports[0]["events"] == [{"uid": "x"}]


def test_a_refusal_is_reported_so_the_page_can_say_where_to_allow_it() -> None:
    calls = Calls(answer=helper.DENIED)
    assert calls.tick(WANTED | {"ask": True}, None) is None
    assert calls.reports == [{"access": "denied"}]


def test_allowed_it_syncs_every_ten_minutes_or_when_asked() -> None:
    calls = Calls(status=helper.GRANTED)
    assert calls.tick(WANTED, 1000.0, now=1100.0) == 1000.0, "not due yet"
    assert calls.reports == []
    assert calls.tick(WANTED | {"sync": True}, 1000.0, now=1100.0) == 1100.0
    assert calls.tick(WANTED, 1100.0, now=1100.0 + 600) == 1700.0


def test_access_taken_away_later_is_reported_once() -> None:
    calls = Calls(status=helper.DENIED)
    assert calls.tick(WANTED, 1000.0) is None
    assert calls.tick(WANTED, None) is None
    assert calls.reports == [{"access": "denied"}]


def test_a_calendar_says_whether_macos_lets_the_owner_change_it() -> None:
    home = helper.calendar_payload(Named("Home", Named("iCloud")), colour=lambda c: "#3366cc")
    holidays = helper.calendar_payload(Named("Holidays", writable=False), colour=lambda c: "")
    assert home == {
        "id": "id-Home",
        "name": "Home",
        "account": "iCloud",
        "writable": True,
        "color": "#3366cc",
    }
    assert holidays["writable"] is False


def test_the_owners_edits_are_made_then_the_calendar_read_again() -> None:
    """CAL9: an edit shows at once, without waiting ten minutes."""
    calls = Calls(status=helper.GRANTED)
    assert calls.tick(WANTED | {"changes": True}, 1000.0, now=1005.0) == 1005.0
    assert calls.edited == 1 and calls.reports[-1]["access"] == "granted"


def test_a_uid_names_its_occurrence() -> None:
    assert helper.occurrence_of("ABC:123@1791360000") == ("ABC:123", 1791360000.0)


class FakeStore:
    def __init__(self, found: Any = None, writable: bool = True) -> None:
        self.saved: list[Any] = []
        self.removed: list[Any] = []
        self.found = found
        self.writable = writable

    def calendarWithIdentifier_(self, identifier: str) -> Any:  # noqa: N802
        return Named(identifier, writable=self.writable)

    def predicateForEventsWithStartDate_endDate_calendars_(self, *args: Any) -> Any:  # noqa: N802
        return args

    def eventsMatchingPredicate_(self, predicate: Any) -> list[Any]:  # noqa: N802
        return [self.found] if self.found is not None else []

    def eventWithIdentifier_(self, identifier: str) -> Any:  # noqa: N802
        return None

    def saveEvent_span_commit_error_(self, event: Any, *rest: Any) -> tuple[bool, None]:  # noqa: N802
        self.saved.append(event)
        return True, None

    def removeEvent_span_commit_error_(self, event: Any, *rest: Any) -> tuple[bool, None]:  # noqa: N802
        self.removed.append(event)
        return True, None


class Editable:
    def __init__(self) -> None:
        self.set: dict[str, Any] = {}

    def eventIdentifier(self) -> str:  # noqa: N802
        return "ABC"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("set") and name.endswith("_"):
            return lambda value: self.set.__setitem__(name[3:-1], value)
        raise AttributeError(name)


def mac_with(store: FakeStore) -> Any:
    from types import SimpleNamespace

    mac = helper.MacCalendars.__new__(helper.MacCalendars)
    made = Editable()
    mac.kit = SimpleNamespace(
        EKEventStore=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: store)),
        EKEvent=SimpleNamespace(eventWithEventStore_=lambda s: made),
    )
    mac.foundation = SimpleNamespace(
        NSDate=SimpleNamespace(dateWithTimeIntervalSince1970_=lambda seconds: seconds)
    )
    mac.made = made
    return mac


EDIT = {
    "calendar_id": "id-Home",
    "title": "Dentist",
    "starts_at": "2026-10-08T09:00:00+02:00",
    "ends_at": "2026-10-08T10:00:00+02:00",
    "location": "Main St",
}


def test_a_new_event_is_saved_in_the_chosen_calendar() -> None:
    store = FakeStore()
    mac = mac_with(store)
    assert mac.apply({"action": "create", "payload": EDIT}) == (True, "")
    made = store.saved[0].set
    assert made["Title"] == "Dentist" and made["Calendar"].title() == "id-Home"
    assert made["StartDate"] == datetime(2026, 10, 8, 7, tzinfo=UTC).timestamp()


def test_an_edit_or_delete_finds_its_occurrence_and_a_read_only_calendar_refuses() -> None:
    found = Editable()
    store = FakeStore(found=found)
    mac = mac_with(store)
    assert mac.apply({"action": "update", "payload": EDIT | {"uid": "ABC@1791399600"}})[0]
    assert found.set["Title"] == "Dentist"
    assert mac.apply({"action": "delete", "payload": {"uid": "ABC@1791399600"}}) == (True, "")
    assert store.removed == [found]
    missing = mac_with(FakeStore(found=None))
    assert missing.apply({"action": "delete", "payload": {"uid": "X@1"}}) == (
        False,
        "event_not_found",
    )
    locked = mac_with(FakeStore(writable=False))
    assert locked.apply({"action": "create", "payload": EDIT}) == (False, "calendar_read_only")


def test_an_empty_read_when_access_is_granted_is_not_sent() -> None:
    """8 Oct: a stale helper read no calendars, and the office's copy was cleared."""
    import pytest

    calls = Calls(status=helper.GRANTED)
    calls.read = lambda: ([], [])  # type: ignore[method-assign]
    with pytest.raises(helper.Stale):
        calls.tick(WANTED | {"sync": True}, 1000.0)
    assert calls.reports == []
