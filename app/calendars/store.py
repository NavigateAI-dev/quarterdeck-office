"""The owner's connected calendars: connect, sync, read their day.

Read only. A link or password goes into the secret store and nowhere else;
each sync replaces a source's events within its window, so an event moved or
deleted in the calendar moves or goes here too. The owner sees everything on
their own screens; what agents see is cut to the owner's choice (agent_view).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.calendars import oauth
from app.calendars.fetch import CalendarFetchError, caldav_calendars, caldav_events, fetch_ics
from app.calendars.parse import CalendarUnreadable, Event, events_from_ics
from app.db.models import CalendarChange, CalendarEvent, CalendarSource, OfficeSettings
from app.secret_store import SecretStore, SecretStoreError

logger = logging.getLogger("quarterdeck.calendars")

BEHIND = timedelta(days=62)
AHEAD = timedelta(days=400)
EVERY = timedelta(minutes=10)
KINDS = ("ics", "caldav")
PROVIDERS = ("google", "icloud", "outlook", "fastmail", "yahoo", "nextcloud", "other")
# The calendars on the owner's Mac, read by the helper app beside the office
# no link or password, macOS asks the owner itself.
MAC = "mac"
MAC_SECRET = "mac-calendar"
# The helper asks every few seconds; quiet for longer, it is not running.
HELPER_GONE = timedelta(seconds=45)
# A change handed to the helper and not reported on within this may have been
# made: it becomes unknown for the owner to check, and is never handed out again.
SENT_GONE = timedelta(minutes=2)
MAC_ACCESS = ("granted", "denied", "restricted", "unsupported")
# Where each service keeps CalDAV, so the owner gives only their account.
CALDAV_SERVERS = {
    "icloud": "https://caldav.icloud.com/",
    "fastmail": "https://caldav.fastmail.com/dav/",
    "yahoo": "https://caldav.calendar.yahoo.com/",
}
SHARE = ("busy", "titles", "all")
DEFAULTS: dict[str, Any] = {
    "share": "titles",
    "hold": True,
    "prep": True,
    "dismissed": [],
    "timezone": "UTC",
}
MAX_DISMISSED = 200


class CalendarRejected(Exception):
    """The owner's calendar could not be added or used; the code says why."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def window(now: datetime) -> tuple[datetime, datetime]:
    return now - BEHIND, now + AHEAD


def preferences(session: Session) -> dict[str, Any]:
    """The owner's calendar choices, with the defaults they agreed to."""
    row = session.scalar(select(OfficeSettings).limit(1))
    stored = dict(getattr(row, "calendar_json", None) or {})
    chosen = DEFAULTS | {key: stored[key] for key in DEFAULTS if key in stored}
    if chosen["share"] not in SHARE:
        chosen["share"] = DEFAULTS["share"]
    return chosen


def save_preferences(session: Session, changes: dict[str, Any]) -> dict[str, Any]:
    row = session.scalar(select(OfficeSettings).limit(1))
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    stored = dict(row.calendar_json or {})
    for key in ("share", "hold", "prep"):
        if key in changes and changes[key] is not None:
            stored[key] = changes[key]
    if changes.get("timezone"):
        # Only a zone the office knows; the browser says which the owner is in.
        stored["timezone"] = zone_of(changes["timezone"]).key
    if "dismiss" in changes:
        dismissed = [uid for uid in stored.get("dismissed", []) if uid != changes["dismiss"]]
        stored["dismissed"] = (dismissed + [changes["dismiss"]])[-MAX_DISMISSED:]
    row.calendar_json = stored
    session.flush()
    return preferences(session)


def source_view(row: CalendarSource) -> dict[str, Any]:
    """A source as the page shows it: never its link or password."""
    return {
        "id": str(row.id),
        "kind": row.kind,
        "provider": row.provider,
        "name": row.name,
        "username": row.username,
        "calendars": [c.get("name") for c in row.calendars_json or []],
        "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
        "last_error": row.last_error,
        "event_count": row.event_count,
    }


def change_view(change: CalendarChange) -> dict[str, Any]:
    return {
        "id": change.id,
        "action": change.action,
        "payload": dict(change.payload_json or {}),
        "status": change.status,
        "error": change.error,
    }


def event_view(row: CalendarEvent, calendar: dict[str, Any] | None = None) -> dict[str, Any]:
    """An event for the owner's own screens. With its calendar's facts (the
    Mac reports them), it says its colour and whether the owner may edit it."""
    known = calendar or {}
    return {
        "id": row.id,
        "source_id": str(row.source_id),
        "calendar_id": row.calendar_id,
        "color": known.get("color") or "",
        "writable": bool(known.get("writable")),
        "uid": row.uid,
        "calendar": row.calendar,
        "title": row.title,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat(),
        "all_day": row.all_day,
        "busy": row.busy,
        "location": row.location,
        "description": row.description,
        "attendees": row.attendees,
    }


def calendars_by_id(session: Session) -> dict[tuple[str, str], dict[str, Any]]:
    """Each Mac calendar's facts by (source, calendar id): name, colour, writable."""
    known: dict[tuple[str, str], dict[str, Any]] = {}
    # The Mac's calendars, and those of a Google or Microsoft sign-in.
    kinds = (MAC, *oauth.BY_KIND)
    for row in session.scalars(select(CalendarSource).where(CalendarSource.kind.in_(kinds))):
        for calendar in row.calendars_json or []:
            if isinstance(calendar, dict) and calendar.get("id"):
                known[(str(row.id), str(calendar["id"]))] = calendar
    return known


def events_for_owner(session: Session, start: datetime, end: datetime) -> list[dict[str, Any]]:
    known = calendars_by_id(session)
    return [
        event_view(row, known.get((str(row.source_id), row.calendar_id)))
        for row in events_between(session, start, end)
    ]


class Calendars:
    """Connecting and syncing the owner's calendars."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secrets: Callable[[], SecretStore],
        http: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.secrets = secrets
        self.http = http or (lambda: httpx.AsyncClient())
        self.clock = clock or (lambda: datetime.now(UTC))
        # What the Mac helper was last told and last said; held in memory, as
        # the helper asks again within seconds after a restart.
        self.helper_seen: datetime | None = None
        self.helper_ask = False
        self.helper_sync = False
        # Sign-ins to Google or Microsoft under way.
        self.sign_ins = oauth.SignIns()

    # ---- the owner's own sign-in app at Google or Microsoft ----

    @staticmethod
    def _app_name(provider: str) -> str:
        return f"calendar-app-{provider}"

    def apps(self) -> dict[str, bool]:
        """Which companies the owner has registered a sign-in app with."""
        try:
            held = set(self.secrets().names("calendar-app-"))
        except SecretStoreError:
            held = set()
        return {key: self._app_name(key) in held for key in oauth.PROVIDERS}

    def set_app(self, provider: str, client_id: str, client_secret: str = "") -> dict[str, bool]:
        chosen = oauth.PROVIDERS.get(provider)
        if chosen is None or not client_id.strip():
            raise CalendarRejected("sign_in_app_incomplete")
        if chosen.needs_secret and not client_secret.strip():
            raise CalendarRejected("sign_in_app_incomplete")
        app = {"client_id": client_id.strip(), "client_secret": client_secret.strip()}
        try:
            self.secrets().put(self._app_name(provider), json.dumps(app))
        except SecretStoreError:
            raise CalendarRejected("calendar_secret_unavailable") from None
        return self.apps()

    def _app(self, provider: str) -> dict[str, str]:
        try:
            return dict(json.loads(self.secrets().get(self._app_name(provider))))
        except (SecretStoreError, ValueError):
            raise CalendarRejected("sign_in_app_missing") from None

    def begin_sign_in(self, provider: str, origin: str) -> str:
        """The company's consent page, for the owner's browser to open."""
        chosen = oauth.PROVIDERS.get(provider)
        if chosen is None:
            raise CalendarRejected("calendar_kind_unknown")
        app = self._app(provider)
        return self.sign_ins.begin(
            chosen, app["client_id"], oauth.redirect_for(origin), f"{origin.rstrip('/')}/#calendars"
        )

    async def finish_sign_in(self, pending: oauth.Pending, code: str) -> dict[str, Any]:
        """The company sent the owner back: tokens sealed, calendars read."""
        chosen = oauth.PROVIDERS[pending.provider]
        app = self._app(chosen.key)
        async with self.http() as http:
            held = await oauth.finish(chosen, app, code, pending, http)
            email, calendars = await oauth.account(chosen, str(held["access_token"]), http)
        if not oauth.may_change(chosen, held):
            # Consent to reading only: every calendar stays read-only.
            calendars = [c | {"writable": False} for c in calendars]
        if not calendars:
            raise CalendarRejected("calendar_none_found")
        # Signing in again to the same account renews that connection (to allow
        # changes, say) rather than adding a second copy of its calendars.
        with self.session_factory() as session:
            again = session.scalar(
                select(CalendarSource).where(
                    CalendarSource.kind == chosen.kind, CalendarSource.username == (email or None)
                )
            )
            known = (again.id, again.secret_name) if again is not None else None
        source_id = known[0] if known else uuid4()
        secret_name = known[1] if known else f"calendar-{source_id.hex}"
        try:
            self.secrets().put(secret_name, json.dumps(held))
        except SecretStoreError:
            raise CalendarRejected("calendar_secret_unavailable") from None
        with self.session_factory() as session, session.begin():
            row = session.get(CalendarSource, source_id)
            if row is None:
                row = CalendarSource(
                    id=source_id,
                    kind=chosen.kind,
                    provider="google" if chosen.key == "google" else "outlook",
                    name=chosen.name,
                    username=email or None,
                    secret_name=secret_name,
                )
                session.add(row)
            row.calendars_json = calendars
            row.last_error = None
        synced = await self.sync(source_id)
        return synced or {}

    async def _read_signed_in(
        self, kind: str, secret_name: str, held_text: str, calendars: list[Any]
    ) -> list[Event]:
        chosen = oauth.BY_KIND[kind]
        app = self._app(chosen.key)
        start, end = window(self.clock())
        try:
            held = dict(json.loads(held_text))
        except ValueError:
            raise CalendarRejected("calendar_secret_unavailable") from None
        async with self.http() as http:
            token, renewed = await oauth.access_token(chosen, app, held, http)
            if renewed is not None:
                self.secrets().put(secret_name, json.dumps(renewed))
            return await oauth.events(chosen, token, calendars, start, end, http)

    def helper_running(self) -> bool:
        return self.helper_seen is not None and self.clock() - self.helper_seen < HELPER_GONE

    def _mac_row(self, session: Session) -> CalendarSource | None:
        return session.scalar(select(CalendarSource).where(CalendarSource.kind == MAC).limit(1))

    def use_mac(self) -> dict[str, Any]:
        """The owner's press: the helper asks macOS, and the dialog is macOS's own."""
        with self.session_factory() as session, session.begin():
            row = self._mac_row(session)
            if row is None:
                row = CalendarSource(
                    kind=MAC, provider=MAC, name="This Mac", secret_name=MAC_SECRET
                )
                session.add(row)
                session.flush()
            row.last_error = None
            self.helper_ask = True
            return self.mac_view(row)

    def mac_view(self, row: CalendarSource) -> dict[str, Any]:
        return source_view(row) | {"helper": "running" if self.helper_running() else "not_running"}

    def helper_poll(self) -> dict[str, bool]:
        """What the helper should do now; asking also says it is running."""
        self.helper_seen = self.clock()
        with self.session_factory() as session:
            row = self._mac_row(session)
            changes = row is not None and (
                session.scalar(
                    select(CalendarChange.id)
                    .where(CalendarChange.source_id == row.id, CalendarChange.status == "pending")
                    .limit(1)
                )
                is not None
            )
        wanted = row is not None
        return {
            "wanted": wanted,
            "ask": wanted and self.helper_ask,
            "sync": wanted and self.helper_sync,
            "changes": bool(changes),
        }

    # ---- the owner's edits ----

    def queue_change(
        self, action: str, fields: dict[str, Any], event_id: int | None = None
    ) -> dict[str, Any]:
        """An edit to apply: only to a Mac calendar macOS lets the owner change."""
        with self.session_factory() as session, session.begin():
            known = calendars_by_id(session)
            payload = dict(fields)
            source: CalendarSource | None
            if event_id is not None:
                event = session.get(CalendarEvent, event_id)
                if event is None:
                    raise CalendarRejected("event_not_found")
                source = session.get(CalendarSource, event.source_id)
                payload["uid"] = event.uid
                payload["from_id"] = event.calendar_id
                payload["calendar_id"] = payload.get("calendar_id") or event.calendar_id
                needed = {event.calendar_id, str(payload["calendar_id"])}
            else:
                # The source holding the chosen calendar: the Mac, or a sign-in.
                wanted = str(payload.get("calendar_id") or "")
                owner = next(
                    (source_id for (source_id, calendar_id) in known if calendar_id == wanted),
                    None,
                )
                source = session.get(CalendarSource, UUID(owner)) if owner else None
                needed = {wanted}
            if source is None or source.kind not in (MAC, *oauth.BY_KIND):
                raise CalendarRejected("calendar_read_only")
            # The calendar it is in, and the one it moves to, must both allow it.
            for calendar_id in needed:
                calendar = known.get((str(source.id), calendar_id))
                if not calendar or not calendar.get("writable"):
                    raise CalendarRejected("calendar_read_only")
            change = CalendarChange(source_id=source.id, action=action, payload_json=payload)
            session.add(change)
            session.flush()
            # A sign-in's calendar is changed by the office itself, at once;
            # the Mac's waits for the helper.
            return change_view(change) | {"direct": source.kind in oauth.BY_KIND}

    async def apply_now(self, change_id: int) -> dict[str, Any] | None:
        """Make a change in a Google or Microsoft calendar, then read it again."""
        with self.session_factory() as session:
            change = session.get(CalendarChange, change_id)
            if change is None:
                return None
            source = session.get(CalendarSource, change.source_id)
            if source is None or source.kind not in oauth.BY_KIND:
                return change_view(change)
            kind, secret_name, source_id = source.kind, source.secret_name, source.id
            action, wanted = change.action, dict(change.payload_json or {})
            zone = zone_of(preferences(session).get("timezone"))
        chosen = oauth.BY_KIND[kind]
        try:
            app = self._app(chosen.key)
            held = dict(json.loads(self.secrets().get(secret_name)))
            async with self.http() as http:
                token, renewed = await oauth.access_token(chosen, app, held, http)
                if renewed is not None:
                    self.secrets().put(secret_name, json.dumps(renewed))
                await oauth.change_event(chosen, token, action, wanted, zone, http)
            ok, error = True, ""
        except oauth.SignInError as failed:
            if failed.code == oauth.NO_ANSWER:
                # Not "failed": it may have been made. The owner checks; it is
                # not sent again.
                return self.change_applied(change_id, False, failed.code, unknown=True)
            ok, error = False, failed.code
        except CalendarRejected as failed:
            ok, error = False, failed.code
        except (SecretStoreError, ValueError):
            ok, error = False, "calendar_secret_unavailable"
        self.change_applied(change_id, ok, error)
        if ok:
            await self.sync(source_id)
        return self.change(change_id)

    def pending_changes(self) -> list[dict[str, Any]]:
        """The changes for the helper to make, each handed out once: marked sent."""
        self.helper_seen = self.clock()
        with self.session_factory() as session, session.begin():
            row = self._mac_row(session)
            if row is None:
                return []
            self._expire_sent(session)
            changes = list(
                session.scalars(
                    select(CalendarChange)
                    .where(CalendarChange.source_id == row.id, CalendarChange.status == "pending")
                    .order_by(CalendarChange.id)
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            )
            for change in changes:
                change.status, change.sent_at = "sent", self.clock()
            return [change_view(change) for change in changes]

    def _expire_sent(self, session: Session) -> None:
        """Sent and never reported on: it may have been made, so the owner checks."""
        stale = session.scalars(
            select(CalendarChange).where(
                CalendarChange.status == "sent",
                CalendarChange.sent_at < self.clock() - SENT_GONE,
            )
        )
        for change in stale:
            change.status, change.error = "unknown", "calendar_no_answer"
            change.done_at = self.clock()

    def change_applied(
        self, change_id: int, ok: bool, error: str = "", unknown: bool = False
    ) -> dict[str, Any] | None:
        self.helper_seen = self.clock()
        with self.session_factory() as session, session.begin():
            change = session.get(CalendarChange, change_id)
            if change is None:
                return None
            change.status = "done" if ok else "unknown" if unknown else "failed"
            change.error = None if ok else (error or "calendar_change_failed")[:64]
            change.done_at = self.clock()
            return change_view(change)

    def change(self, change_id: int) -> dict[str, Any] | None:
        with self.session_factory() as session, session.begin():
            self._expire_sent(session)
            found = session.get(CalendarChange, change_id)
            return change_view(found) if found is not None else None

    def helper_report(
        self, access: str, calendars: list[dict[str, Any]], events: list[Event]
    ) -> dict[str, Any] | None:
        """What the helper found: macOS's answer, and the events when allowed."""
        self.helper_seen = self.clock()
        self.helper_ask = False
        self.helper_sync = False
        with self.session_factory() as session, session.begin():
            row = self._mac_row(session)
            if row is None:
                return None
            if access != "granted":
                row.last_error = f"mac_access_{access}"
                return self.mac_view(row)
            if not calendars and row.calendars_json:
                # A Mac that had calendars showing none is a hiccup, not the
                # owner clearing them: the last good copy stays.
                return self.mac_view(row)
            row.calendars_json = calendars
            start, end = window(self.clock())
            self._replace(
                session, row, [e for e in events if e.ends_at > start and e.starts_at < end]
            )
            return self.mac_view(row)

    async def _read(
        self, kind: str, secret: str, server: str | None, user: str | None, calendars: list[Any]
    ) -> list[Event]:
        start, end = window(self.clock())
        async with self.http() as http:
            if kind == "ics":
                return events_from_ics(await fetch_ics(secret, http), start, end)
            found: list[Event] = []
            for calendar in calendars:
                for text in await caldav_events(
                    calendar["url"], user or "", secret, start, end, http
                ):
                    found.extend(events_from_ics(text, start, end, calendar.get("name", "")))
            return found

    async def connect(
        self,
        kind: str,
        provider: str,
        name: str,
        url: str = "",
        username: str = "",
        password: str = "",
        server_url: str = "",
    ) -> dict[str, Any]:
        """Add a calendar after reading it once, so a wrong link is said at once."""
        if kind not in KINDS or provider not in PROVIDERS:
            raise CalendarRejected("calendar_kind_unknown")
        calendars: list[dict[str, str]] = []
        server = None
        try:
            if kind == "ics":
                if not url:
                    raise CalendarRejected("calendar_address_missing")
                secret = url.strip()
                events = await self._read(kind, secret, None, None, [])
            else:
                server = (server_url or CALDAV_SERVERS.get(provider, "")).strip()
                if not server or not username or not password:
                    raise CalendarRejected("calendar_account_incomplete")
                secret = password
                async with self.http() as http:
                    calendars = await caldav_calendars(server, username, password, http)
                if not calendars:
                    raise CalendarRejected("calendar_none_found")
                events = await self._read(kind, secret, server, username, calendars)
        except CalendarFetchError as failed:
            raise CalendarRejected(failed.code) from None
        except CalendarUnreadable as failed:
            raise CalendarRejected(str(failed)) from None
        source_id = uuid4()
        secret_name = f"calendar-{source_id.hex}"
        try:
            self.secrets().put(secret_name, secret)
        except SecretStoreError:
            raise CalendarRejected("calendar_secret_unavailable") from None
        with self.session_factory() as session, session.begin():
            row = CalendarSource(
                id=source_id,
                kind=kind,
                provider=provider,
                name=(name.strip() or provider.title())[:255],
                server_url=server,
                username=username.strip() or None,
                secret_name=secret_name,
                calendars_json=calendars,
            )
            session.add(row)
            session.flush()
            self._replace(session, row, events)
            return source_view(row)

    def _replace(self, session: Session, row: CalendarSource, events: list[Event]) -> None:
        session.execute(delete(CalendarEvent).where(CalendarEvent.source_id == row.id))
        seen: set[str] = set()
        for event in events:
            if event.uid in seen:
                continue
            seen.add(event.uid)
            values = asdict(event)
            values["uid"] = values["uid"][:512]
            values["calendar"] = values["calendar"][:255]
            session.add(CalendarEvent(source_id=row.id, **values))
        row.event_count = len(seen)
        row.last_synced_at = self.clock()
        row.last_error = None

    async def sync(self, source_id: UUID) -> dict[str, Any] | None:
        """Read one source again; a failure is kept on its card, its events stay."""
        with self.session_factory() as session:
            row = session.get(CalendarSource, source_id)
            if row is None:
                return None
            if row.kind == MAC:
                # The helper on the Mac reads it, on its next ask.
                self.helper_sync = True
                return self.mac_view(row)
            kind, secret_name = row.kind, row.secret_name
            server, user, calendars = row.server_url, row.username, list(row.calendars_json or [])
        try:
            secret = self.secrets().get(secret_name)
            if kind in oauth.BY_KIND:
                events = await self._read_signed_in(kind, secret_name, secret, calendars)
            else:
                events = await self._read(kind, secret, server, user, calendars)
            error = None
        except (CalendarFetchError, CalendarRejected, oauth.SignInError) as failed:
            events, error = None, failed.code
        except CalendarUnreadable as failed:
            events, error = None, str(failed)
        except SecretStoreError:
            events, error = None, "calendar_secret_unavailable"
        with self.session_factory() as session, session.begin():
            row = session.get(CalendarSource, source_id)
            if row is None:
                return None
            if events is None:
                row.last_error = error
            else:
                self._replace(session, row, events)
            return source_view(row)

    async def sync_due(self) -> int:
        """Sync every source last read more than EVERY ago. Returns how many."""
        due_before = self.clock() - EVERY
        with self.session_factory() as session:
            ids = [
                row.id
                for row in session.scalars(select(CalendarSource))
                if row.kind != MAC
                and (row.last_synced_at is None or row.last_synced_at < due_before)
            ]
        for source_id in ids:
            try:
                await self.sync(source_id)
            except Exception:  # noqa: BLE001 - one calendar must not stop the others
                logger.exception('{"event":"calendar_sync_failed","source":"%s"}', source_id)
        return len(ids)

    def disconnect(self, source_id: UUID) -> bool:
        with self.session_factory() as session, session.begin():
            row = session.get(CalendarSource, source_id)
            if row is None:
                return False
            secret_name = row.secret_name
            session.delete(row)
        if secret_name == MAC_SECRET:
            return True
        try:
            self.secrets().delete(secret_name)
        except SecretStoreError:
            logger.warning('{"event":"calendar_secret_not_removed"}')
        return True

    def sources(self) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(select(CalendarSource).order_by(CalendarSource.created_at))
            return [self.mac_view(row) if row.kind == MAC else source_view(row) for row in rows]


def events_between(session: Session, start: datetime, end: datetime) -> list[CalendarEvent]:
    """Events touching the span, from every connected calendar, earliest first."""
    return list(
        session.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.starts_at < end, CalendarEvent.ends_at > start)
            .order_by(CalendarEvent.starts_at, CalendarEvent.title)
        )
    )


def owner_status(session: Session, now: datetime) -> dict[str, Any]:
    """Free, in a meeting until a time, or out for the day; and what is next."""
    today = events_between(session, now - timedelta(days=1), now + timedelta(days=1))
    current = [e for e in today if e.busy and e.starts_at <= now < e.ends_at]
    timed = [e for e in current if not e.all_day]
    following = [e for e in today if e.busy and not e.all_day and e.starts_at > now]
    upcoming = event_view(following[0]) if following else None
    if timed:
        # Back-to-back meetings read as one: busy until the last one ends.
        until = max(e.ends_at for e in timed)
        for event in sorted(following, key=lambda e: e.starts_at):
            if event.starts_at <= until:
                until = max(until, event.ends_at)
        return {
            "state": "busy",
            "until": until.isoformat(),
            "title": timed[0].title,
            "next": upcoming,
        }
    if current:
        return {"state": "out", "until": None, "title": current[0].title, "next": upcoming}
    return {"state": "free", "until": None, "title": None, "next": upcoming}


def zone_of(name: str | None) -> ZoneInfo:
    """The owner's time zone, as their browser names it; UTC if unknown."""
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def agent_view(session: Session, now: datetime, days: int = 2) -> str:
    """The owner's schedule as agents may read it, cut to the owner's choice.

    In the owner's own time zone, with the time now, from the start of today:
    an agent asked "what is next?" needs both. "busy": when the owner
    is taken, never what it is. "titles": titles and times; descriptions,
    attendees and places stay on this computer. "all": everything, which is
    sent to the model providers with the task or the conversation.
    """
    if session.scalar(select(CalendarSource.id).limit(1)) is None:
        return ""
    prefs = preferences(session)
    share = prefs["share"]
    zone = zone_of(prefs.get("timezone"))
    here = now.astimezone(zone)
    today = here.replace(hour=0, minute=0, second=0, microsecond=0)
    found = events_between(session, today, today + timedelta(days=days + 1))
    header = (
        f"The owner's calendar, read from their own calendars (times in {zone.key}). "
        f"Now it is {here:%A %d %B %Y, %H:%M}."
    )
    if not found:
        return f"{header}\nNothing on it today or in the next {days} days."
    lines = []
    for event in found[:60]:
        starts, ends = event.starts_at.astimezone(zone), event.ends_at.astimezone(zone)
        when = (
            f"{starts:%a %d %b} (all day)"
            if event.all_day
            else f"{starts:%a %d %b %H:%M}-{ends:%H:%M}"
        )
        state = "busy" if event.busy else "free"
        if not event.all_day and ends <= here:
            state += ", over"
        if share == "busy":
            lines.append(f"- {when}: {state}")
        elif share == "titles":
            lines.append(f"- {when}: {event.title} ({state})")
        else:
            extra = "; ".join(
                part
                for part in (
                    event.location and f"at {event.location}",
                    event.attendees and f"{event.attendees} attendees",
                    event.description and event.description[:300],
                )
                if part
            )
            lines.append(f"- {when}: {event.title} ({state}){'; ' + extra if extra else ''}")
    seen = {"busy": "only when the owner is busy or free", "titles": "titles and times only"}
    limit = seen.get(share, "everything the owner allowed")
    return f"{header} You may see {limit}.\n" + "\n".join(lines)


def holding(session: Session, now: datetime) -> bool:
    """Whether the agents' notifications wait: the owner is in a meeting and
    chose to hold the in-tray then. They go once the meeting ends."""
    if not preferences(session)["hold"]:
        return False
    return bool(owner_status(session, now)["state"] == "busy")


PREP_WITHIN = timedelta(hours=4)
PREP_NOT_BEFORE = timedelta(minutes=15)
PREP_COST = 0.30


def prep_offer(session: Session, now: datetime) -> dict[str, Any] | None:
    """The meeting the Chief offers a brief for: the next one within four
    hours, if the owner wants offers, lets agents see titles, and has not
    answered this one already. Nothing starts until they press Start."""
    prefs = preferences(session)
    if not prefs["prep"] or prefs["share"] == "busy":
        return None
    answered = set(prefs["dismissed"])
    for event in events_between(session, now + PREP_NOT_BEFORE, now + PREP_WITHIN):
        if event.all_day or not event.busy or event.starts_at < now + PREP_NOT_BEFORE:
            continue
        if event.uid in answered:
            continue
        return event_view(event) | {"cost": PREP_COST}
    return None


def prep_objective(title: str, when: str) -> str:
    """What a prep brief is asked to be: from what the office knows, no new research."""
    at = f" at {when}" if when else ""
    return (
        f"Prepare me for “{title}”{at}: a short brief of where the related work in this "
        "office stands, what is waiting on me, and what I might raise. Use what the office "
        "already knows; do not research anew, and do not contact anyone."
    )
