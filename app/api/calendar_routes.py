"""The owner's calendars, connected read-only."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from app import accounts
from app.api.schemas import (
    CalendarChangeResult,
    CalendarConnect,
    CalendarEventEdit,
    CalendarPreferences,
    CalendarPrep,
    CalendarSignInApp,
    CalendarSignInStart,
    MacCalendarReport,
)
from app.calendars.oauth import SignInError
from app.calendars.parse import Event
from app.calendars.store import (
    CALDAV_SERVERS,
    PREP_COST,
    CalendarRejected,
    Calendars,
    calendars_by_id,
    events_for_owner,
    owner_status,
    preferences,
    prep_objective,
    prep_offer,
    save_preferences,
)
from app.db.models import CalendarEvent
from app.office_settings import MonthlyLimitReached, OfficePaused
from app.secret_store import SecretStoreError

# The longest span one request may read: a year, for the year view.
MAX_SPAN = timedelta(days=400)


def add_calendar_routes(
    router: APIRouter,
    session_factory: sessionmaker[Session],
    calendars: Calendars,
    start: Callable[[str, float], Any] | None = None,
) -> None:
    """The calendar routes. `start` begins a prep brief (objective, cost cap)
    and returns the run; the owner's press on Start is what calls it."""

    def chosen(session: Session) -> dict[str, Any]:
        prefs = preferences(session)
        return {key: prefs[key] for key in ("share", "hold", "prep")}

    @router.get("/calendars")
    def list_calendars() -> dict[str, Any]:
        with session_factory() as session:
            prefs = chosen(session)
        return {
            "sources": calendars.sources(),
            "preferences": prefs,
            "caldav_servers": CALDAV_SERVERS,
            "apps": calendars.apps(),
        }

    @router.put("/calendars/apps/{provider}")
    def set_sign_in_app(provider: str, body: CalendarSignInApp) -> dict[str, bool]:
        """The owner's own Google sign-in or Microsoft app registration, sealed."""
        try:
            return calendars.set_app(provider, body.client_id, body.client_secret)
        except CalendarRejected as rejected:
            raise HTTPException(status_code=422, detail=rejected.code) from None

    @router.post("/calendars/oauth/{provider}/start")
    def start_sign_in(provider: str, body: CalendarSignInStart) -> dict[str, str]:
        """The company's consent page, for the owner's browser to go to."""
        try:
            return {"url": calendars.begin_sign_in(provider, body.origin)}
        except CalendarRejected as rejected:
            raise HTTPException(status_code=422, detail=rejected.code) from None

    @router.post("/calendars", status_code=201)
    async def connect_calendar(body: CalendarConnect) -> dict[str, Any]:
        try:
            return await calendars.connect(
                body.kind,
                body.provider,
                body.name,
                url=body.url,
                username=body.username,
                password=body.password,
                server_url=body.server_url,
            )
        except CalendarRejected as rejected:
            raise HTTPException(status_code=422, detail=rejected.code) from None

    @router.post("/calendars/mac", status_code=201)
    def use_mac_calendars() -> dict[str, Any]:
        """The owner's press: the helper on the Mac asks macOS for access."""
        return calendars.use_mac()

    @router.get("/calendars/mac/helper")
    def mac_helper_poll() -> dict[str, bool]:
        """The helper's question, every few seconds: is it wanted, and for what."""
        return calendars.helper_poll()

    @router.post("/calendars/mac/helper")
    def mac_helper_report(body: MacCalendarReport) -> dict[str, Any]:
        events = [
            Event(
                uid=item.uid,
                calendar=item.calendar,
                calendar_id=item.calendar_id,
                title=item.title or "(no title)",
                starts_at=item.starts_at.astimezone(UTC),
                ends_at=item.ends_at.astimezone(UTC),
                all_day=item.all_day,
                busy=item.busy,
                location=item.location,
                description=item.description,
                attendees=item.attendees,
            )
            for item in body.events
        ]
        calendars_seen = [c.model_dump() for c in body.calendars]
        reported = calendars.helper_report(body.access, calendars_seen, events)
        if reported is None:
            raise HTTPException(status_code=404, detail="Calendar not found")
        return reported

    # ---- the owner's edits ----

    def edit_fields(body: CalendarEventEdit) -> dict[str, Any]:
        fields = body.model_dump(mode="json")
        return fields

    def queued(action: str, fields: dict[str, Any], event_id: int | None = None) -> dict[str, Any]:
        try:
            return calendars.queue_change(action, fields, event_id)
        except CalendarRejected as rejected:
            code = 404 if rejected.code == "event_not_found" else 409
            raise HTTPException(status_code=code, detail=rejected.code) from None

    @router.get("/calendars/writable")
    def writable_calendars() -> list[dict[str, Any]]:
        """The calendars the owner may add to: the Mac's, where macOS allows it."""
        with session_factory() as session:
            return [
                {"id": calendar_id, "source_id": source_id} | facts
                for (source_id, calendar_id), facts in calendars_by_id(session).items()
                if facts.get("writable")
            ]

    async def made(change: dict[str, Any]) -> dict[str, Any]:
        """A sign-in's calendar is changed now; the Mac's helper does its own."""
        if change.get("direct"):
            return await calendars.apply_now(int(change["id"])) or change
        return change

    @router.post("/calendars/events", status_code=202)
    async def create_event(body: CalendarEventEdit) -> dict[str, Any]:
        return await made(queued("create", edit_fields(body)))

    @router.put("/calendars/events/{event_id}", status_code=202)
    async def update_event(event_id: int, body: CalendarEventEdit) -> dict[str, Any]:
        return await made(queued("update", edit_fields(body), event_id))

    @router.delete("/calendars/events/{event_id}", status_code=202)
    async def delete_event(event_id: int) -> dict[str, Any]:
        return await made(queued("delete", {}, event_id))

    @router.get("/calendars/changes/{change_id}")
    def calendar_change(change_id: int) -> dict[str, Any]:
        found = calendars.change(change_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Change not found")
        return found

    @router.get("/calendars/mac/helper/changes")
    def mac_helper_changes() -> list[dict[str, Any]]:
        """The owner's edits for the helper to make through macOS."""
        return calendars.pending_changes()

    @router.post("/calendars/mac/helper/changes/{change_id}")
    def mac_helper_change_done(change_id: int, body: CalendarChangeResult) -> dict[str, Any]:
        done = calendars.change_applied(change_id, body.ok, body.error)
        if done is None:
            raise HTTPException(status_code=404, detail="Change not found")
        return done

    @router.post("/calendars/{source_id}/sync")
    async def sync_calendar(source_id: UUID) -> dict[str, Any]:
        synced = await calendars.sync(source_id)
        if synced is None:
            raise HTTPException(status_code=404, detail="Calendar not found")
        return synced

    @router.delete("/calendars/{source_id}", status_code=204)
    def disconnect_calendar(source_id: UUID) -> None:
        if not calendars.disconnect(source_id):
            raise HTTPException(status_code=404, detail="Calendar not found")

    @router.put("/calendars/preferences")
    def set_calendar_preferences(body: CalendarPreferences) -> dict[str, Any]:
        with session_factory() as session, session.begin():
            save_preferences(session, body.model_dump(exclude_none=True))
            return chosen(session)

    @router.get("/calendars/day")
    def calendar_day(
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """The owner's events in a span (their own day, from the page's clock)."""
        now = datetime.now(UTC)
        start = (start or now.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(UTC)
        end = (end or start + timedelta(days=1)).astimezone(UTC)
        if end <= start or end - start > MAX_SPAN:
            raise HTTPException(status_code=422, detail="calendar_span_invalid")
        with session_factory() as session:
            events = events_for_owner(session, start, end)
            status = owner_status(session, now)
            prefs = chosen(session)
            offer = prep_offer(session, now)
        return {
            "events": events,
            "status": status,
            "hold": prefs["hold"],
            "prep": offer,
            "connected": len(calendars.sources()),
        }

    def answered(session: Session, event_id: int) -> CalendarEvent:
        event = session.get(CalendarEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        save_preferences(session, {"dismiss": event.uid})
        return event

    @router.post("/calendars/prep/{event_id}/dismiss", status_code=204)
    def dismiss_prep(event_id: int) -> None:
        with session_factory() as session, session.begin():
            answered(session, event_id)

    @router.post("/calendars/prep/{event_id}/start", status_code=202)
    def start_prep(event_id: int, body: CalendarPrep) -> dict[str, Any]:
        """The owner pressed Start on a prep offer: a brief, capped at its cost."""
        if start is None:
            raise HTTPException(status_code=409, detail="prep_unavailable")
        with session_factory() as session, session.begin():
            title = answered(session, event_id).title
        try:
            run = start(prep_objective(title, body.when), PREP_COST)
        except OfficePaused:
            raise HTTPException(status_code=409, detail="office_paused") from None
        except MonthlyLimitReached:
            raise HTTPException(status_code=409, detail="monthly_limit_reached") from None
        return {"run_id": str(run.id)}


def build_calendar_callback_router(calendars: Calendars) -> APIRouter:
    """Where Google or Microsoft send the owner back after their consent.

    Not behind the owner's token: it is the browser arriving from the company's
    page. The state, made for one sign-in and good once for ten minutes, is
    what makes it the owner's; without it nothing happens.
    """
    router = APIRouter()

    @router.get("/calendars/oauth/callback")
    async def sign_in_returned(
        state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        try:
            pending = calendars.sign_ins.take(state)
        except SignInError:
            return RedirectResponse("/#calendars/sign_in_expired", status_code=303)
        back = pending.back_to
        if error or not code:
            return RedirectResponse(f"{back}/sign_in_declined", status_code=303)
        if pending.provider in accounts.BY_PROVIDER:
            # The owner's mail and files, on the calendar's way back.
            try:
                await accounts.finish(calendars, pending, code, "owner")
            except (
                SignInError,
                CalendarRejected,
                SecretStoreError,
                accounts.AccountError,
            ) as failed:
                return RedirectResponse(
                    f"{back}/{getattr(failed, 'code', 'sign_in_failed')}", status_code=303
                )
            return RedirectResponse(f"{back}/connected", status_code=303)
        try:
            await calendars.finish_sign_in(pending, code)
        except (SignInError, CalendarRejected) as failed:
            return RedirectResponse(f"{back}/{failed.code}", status_code=303)
        return RedirectResponse(f"{back}/connected", status_code=303)

    return router
