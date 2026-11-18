"""The calendars on the owner's Mac, read for the office.

    python -m scripts.mac_calendar start    # build the helper app if needed, start it
    python -m scripts.mac_calendar stop
    python -m scripts.mac_calendar status

The office runs in a container and cannot see the Mac's Calendar app, so a
small app of its own, "Quarterdeck Calendar", reads it. macOS gives calendar
access per app and asks the owner in its own dialog, naming the app; a bare
script would be refused without asking, or be named after whatever started it.

The helper asks the office every few seconds whether it is wanted. It asks
macOS for access only after the owner pressed "Use the calendars on this Mac",
then sends the events of the coming month (and the past week) every ten
minutes. It makes the owner's own edits from the office's calendar view, each
handed to it once, and says how each went; one it never reports on becomes
"unknown" for the owner to check, and is not sent again. Its owner token is read from the
office's own .env on this computer and sent only to the office on this Mac.
"""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import plistlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("OFFICE_URL", "http://127.0.0.1:8000")
HOME = ROOT / ".calendar-helper"
APP = HOME / "Quarterdeck Calendar.app"
LOCK = HOME / "helper.lock"
LOG = HOME / "helper.log"
BUNDLE_ID = "ai.navigate.quarterdeck.calendar"
WHY = (
    "So your office colleagues know when you are free and work around your day. "
    "Read only: nothing is ever added or changed."
)
ASK_EVERY = 5.0
AWAY_EVERY = 15.0
SYNC_EVERY = timedelta(minutes=10)
BEHIND = timedelta(days=62)
AHEAD = timedelta(days=400)
# EventKit's answers, as the office words them.
GRANTED, DENIED, RESTRICTED, UNKNOWN = "granted", "denied", "restricted", "not_asked"
STATUS = {0: UNKNOWN, 1: RESTRICTED, 2: DENIED, 3: GRANTED, 4: DENIED}
CANCELED = 3
FREE = 1


# ---- the app macOS asks about ----


def info_plist() -> bytes:
    return plistlib.dumps(
        {
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": "Quarterdeck Calendar",
            "CFBundleDisplayName": "Quarterdeck Calendar",
            "CFBundleExecutable": "helper",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            # No Dock icon, no menu: it only reads, in the background.
            "LSUIElement": True,
            "NSCalendarsFullAccessUsageDescription": WHY,
            "NSCalendarsUsageDescription": WHY,
        }
    )


def launcher_script(root: Path = ROOT) -> str:
    # Kept the same from version to version: macOS ties the owner's answer to
    # this app as signed, so rewriting it would ask the owner again.
    python = root / ".venv" / "bin" / "python"
    log = root / ".calendar-helper" / "helper.log"
    return (
        f'#!/bin/bash\ncd "{root}"\nexec "{python}" -m scripts.mac_calendar run >> "{log}" 2>&1\n'
    )


def install(app: Path = APP, root: Path = ROOT) -> bool:
    """Write the helper app, signed for this Mac, if it is missing or changed."""
    contents = app / "Contents"
    executable = contents / "MacOS" / "helper"
    wanted = {contents / "Info.plist": info_plist(), executable: launcher_script(root).encode()}
    if all(path.exists() and path.read_bytes() == data for path, data in wanted.items()):
        return False
    executable.parent.mkdir(parents=True, exist_ok=True)
    for path, data in wanted.items():
        path.write_bytes(data)
    executable.chmod(0o755)
    subprocess.run(  # noqa: S603 - fixed arguments
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
        check=False,
        capture_output=True,
    )
    return True


# ---- the office on this Mac ----


def owner_token(env_file: Path = ROOT / ".env") -> str:
    for line in env_file.read_text().splitlines():
        if line.startswith("OWNER_API_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("OWNER_API_TOKEN is not in .env; start the office first.")


def office(method: str, path: str, body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - the office on this Mac
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {owner_token()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310
        text = reply.read().decode()
    return json.loads(text) if text else None


# ---- the Calendar app, through EventKit ----


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def event_payload(event: Any) -> dict[str, Any] | None:
    """One EventKit event as the office takes it; a cancelled one is left out."""
    if event.status() == CANCELED:
        return None
    start = float(event.startDate().timeIntervalSince1970())
    end = float(event.endDate().timeIntervalSince1970())
    identifier = event.eventIdentifier() or event.calendarItemIdentifier() or "event"
    calendar = event.calendar()
    return {
        # A repeat's occurrences share an identifier; the start tells them apart.
        "uid": f"{identifier}@{int(start)}"[:512],
        "calendar": str(calendar.title() if calendar is not None else "")[:255],
        "calendar_id": str(calendar.calendarIdentifier() if calendar is not None else "")[:255],
        "title": str(event.title() or "(no title)")[:4000],
        "starts_at": _iso(start),
        "ends_at": _iso(max(end, start)),
        "all_day": bool(event.isAllDay()),
        "busy": event.availability() != FREE,
        "location": str(event.location() or "")[:4000],
        "description": str(event.notes() or "")[:4000],
        "attendees": len(event.attendees() or []),
    }


def colour_of(calendar: Any) -> str:
    """The calendar's colour as #rrggbb, as the Calendar app shows it."""
    try:
        appkit = importlib.import_module("AppKit")
        rgb = calendar.color().colorUsingColorSpace_(appkit.NSColorSpace.sRGBColorSpace())
        parts = (rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent())
        return "#" + "".join(f"{round(max(0.0, min(1.0, part)) * 255):02x}" for part in parts)
    except Exception:  # noqa: BLE001 - a colour is a nicety; none is fine
        return ""


def calendar_payload(calendar: Any, colour: Callable[[Any], str] = colour_of) -> dict[str, Any]:
    source = calendar.source()
    return {
        "id": str(calendar.calendarIdentifier() or "")[:255],
        "name": str(calendar.title() or "")[:255],
        "account": str(source.title() if source is not None else "")[:255],
        # Whether macOS lets the owner change it: a shared or subscribed
        # calendar (holidays, birthdays) does not.
        "writable": bool(calendar.allowsContentModifications()),
        "color": colour(calendar),
    }


def occurrence_of(uid: str) -> tuple[str, float]:
    """The event's identifier and the start of the occurrence its uid names."""
    identifier, _, start = uid.rpartition("@")
    return identifier, float(start) if start.lstrip("-").isdigit() else 0.0


class MacCalendars:
    """EventKit, imported only on a Mac and only when the helper runs."""

    def __init__(self) -> None:
        # Only on a Mac: loaded by name, as neither exists anywhere else.
        self.kit: Any = importlib.import_module("EventKit")
        self.foundation: Any = importlib.import_module("Foundation")

    def status(self) -> str:
        code = int(self.kit.EKEventStore.authorizationStatusForEntityType_(0))
        return STATUS.get(code, DENIED)

    def ask(self) -> str:
        """macOS's own dialog, naming this app. Waits for the owner's answer."""
        store = self.kit.EKEventStore.alloc().init()
        answered = threading.Event()
        result: dict[str, bool] = {}

        def finished(granted: bool, _error: Any) -> None:
            result["granted"] = bool(granted)
            answered.set()

        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(finished)
        else:
            store.requestAccessToEntityType_completion_(0, finished)
        deadline = time.monotonic() + 600
        loop = self.foundation.NSRunLoop.currentRunLoop()
        while not answered.is_set() and time.monotonic() < deadline:
            loop.runUntilDate_(self.foundation.NSDate.dateWithTimeIntervalSinceNow_(0.25))
        return GRANTED if result.get("granted") else self.status()

    def _date(self, value: str) -> Any:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return self.foundation.NSDate.dateWithTimeIntervalSince1970_(moment.timestamp())

    def _find(self, store: Any, uid: str) -> Any:
        """The one occurrence the owner chose, of a repeat or of a single event."""
        identifier, start = occurrence_of(uid)
        date = self.foundation.NSDate.dateWithTimeIntervalSince1970_
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            date(start - 60), date(start + 60), None
        )
        for event in store.eventsMatchingPredicate_(predicate) or []:
            if event.eventIdentifier() == identifier:
                return event
        return store.eventWithIdentifier_(identifier)

    def apply(self, change: dict[str, Any]) -> tuple[bool, str]:
        """The owner's edit, made in the Calendar app's own store.

        Only this occurrence of a repeat is changed, as the Calendar app does
        when asked "only this event".
        """
        store = self.kit.EKEventStore.alloc().init()
        wanted = dict(change.get("payload") or {})
        action = change.get("action")
        if action == "create":
            event = self.kit.EKEvent.eventWithEventStore_(store)
        else:
            event = self._find(store, str(wanted.get("uid") or ""))
            if event is None:
                return False, "event_not_found"
        if action == "delete":
            ok, _ = store.removeEvent_span_commit_error_(event, 0, True, None)
            return bool(ok), "" if ok else "calendar_change_refused"
        calendar = store.calendarWithIdentifier_(str(wanted.get("calendar_id") or ""))
        if calendar is None or not calendar.allowsContentModifications():
            return False, "calendar_read_only"
        event.setCalendar_(calendar)
        event.setTitle_(str(wanted.get("title") or "(no title)"))
        event.setAllDay_(bool(wanted.get("all_day")))
        event.setStartDate_(self._date(str(wanted["starts_at"])))
        event.setEndDate_(self._date(str(wanted["ends_at"])))
        event.setLocation_(str(wanted.get("location") or "") or None)
        event.setNotes_(str(wanted.get("notes") or "") or None)
        ok, _ = store.saveEvent_span_commit_error_(event, 0, True, None)
        return bool(ok), "" if ok else "calendar_change_refused"

    def read(self, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        store = self.kit.EKEventStore.alloc().init()
        calendars = list(store.calendarsForEntityType_(0) or [])
        date = self.foundation.NSDate.dateWithTimeIntervalSince1970_
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            date((now - BEHIND).timestamp()), date((now + AHEAD).timestamp()), calendars
        )
        events = [event_payload(e) for e in store.eventsMatchingPredicate_(predicate) or []]
        return [calendar_payload(c) for c in calendars], [e for e in events if e]


# ---- what the helper does each time it asks ----


class Stale(Exception):
    """macOS says access is granted but shows no calendars at all.

    Seen on 8 Oct after a night: a long-running helper's link to the calendar
    store had gone quiet, and a fresh process read all 27 calendars at once.
    """


def tick(
    poll: dict[str, bool],
    status: Callable[[], str],
    ask: Callable[[], str],
    read: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    report: Callable[[dict[str, Any]], Any],
    last_sync: float | None,
    now: float,
    edits: Callable[[], int] | None = None,
) -> float | None:
    """One round: ask macOS when the owner pressed, read when allowed and due.

    Returns when it last sent the events. Nothing happens unless wanted, and
    macOS is never asked except straight after the owner's press.
    """
    if not poll.get("wanted"):
        return last_sync
    pressed = bool(poll.get("ask"))
    access = ask() if pressed else status()
    if access != GRANTED:
        answer = access if access in (DENIED, RESTRICTED) else DENIED
        if pressed:
            report({"access": answer})  # macOS's answer to the owner's press.
        elif last_sync is not None:
            report({"access": answer})  # Allowed before, taken away since.
        return None
    # The owner's edits first, then a fresh read, so the change shows at once.
    applied = edits() if edits is not None and poll.get("changes") else 0
    due = last_sync is None or now - last_sync >= SYNC_EVERY.total_seconds()
    if not (due or pressed or poll.get("sync") or applied):
        return last_sync
    calendars, events = read()
    if not calendars:
        # Never send an empty calendar as if the owner had cleared it.
        raise Stale
    report({"access": GRANTED, "calendars": calendars, "events": events})
    return now


def run() -> int:
    HOME.mkdir(exist_ok=True)
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # Already running.
    lock.write(str(os.getpid()))
    lock.flush()
    mac = MacCalendars()
    last_sync: float | None = None

    def edits() -> int:
        changes = office("GET", "/calendars/mac/helper/changes") or []
        for change in changes:
            try:
                ok, error = mac.apply(change)
            except Exception:  # noqa: BLE001 - one bad edit must not stop the helper
                ok, error = False, "calendar_change_failed"
            office(
                "POST", f"/calendars/mac/helper/changes/{change['id']}", {"ok": ok, "error": error}
            )
        return len(changes)

    while True:
        try:
            poll = office("GET", "/calendars/mac/helper")
            last_sync = tick(
                poll,
                mac.status,
                mac.ask,
                lambda: mac.read(datetime.now(UTC)),
                lambda body: office("POST", "/calendars/mac/helper", body),
                last_sync,
                time.time(),
                edits,
            )
            time.sleep(ASK_EVERY)
        except Stale:
            # A fresh process reads again; this one hands over and ends.
            lock.close()
            subprocess.run(["/usr/bin/open", "-n", "-g", "-j", str(APP)], check=False)  # noqa: S603
            return 0
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(AWAY_EVERY)  # The office is not up; ask again shortly.


def running() -> int | None:
    if not LOCK.exists():
        return None
    with LOCK.open() as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            text = LOCK.read_text().strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(held, fcntl.LOCK_UN)
    return None


def start() -> int:
    if sys.platform != "darwin":
        print("The calendar helper is for a Mac; this computer is not one.")
        return 0
    if not (ROOT / ".venv" / "bin" / "python").exists():
        print("The calendar helper needs the office's Python environment (.venv).")
        return 1
    HOME.mkdir(exist_ok=True)
    install()
    if running() is not None:
        print("The calendar helper is running.")
        return 0
    # -g: in the background, without taking focus; -j: hidden.
    subprocess.run(["/usr/bin/open", "-g", "-j", str(APP)], check=False)  # noqa: S603
    print("The calendar helper is running.")
    return 0


def stop() -> int:
    pid = running()
    if pid and pid > 0:
        os.kill(pid, 15)
        print("Stopped the calendar helper.")
    else:
        print("The calendar helper was not running.")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "start"
    actions = {"start": start, "stop": stop, "run": run}
    if command == "status":
        print("running" if running() is not None else "not running")
        return 0
    if command not in actions:
        print(__doc__)
        return 2
    return actions[command]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
