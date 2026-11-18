"""The owner's wall of widgets, against the database."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app import widgets
from app.calendars.store import preferences, save_preferences
from app.db.models import CalendarEvent, CalendarSource
from app.secret_store import SecretStoreError
from tests.integration.test_calendars import NOW, calendars, office  # noqa: F401

pytestmark = pytest.mark.integration


async def connected(office: Any) -> str:  # noqa: F811
    added = await calendars(office).connect(
        "ics", "google", "Work", url="https://calendar.example.com/private-abc/basic.ics"
    )
    return str(added["id"])


def test_a_new_office_has_the_floor_it_always_had(office: Any) -> None:  # noqa: F811
    factory, _, _ = office
    with factory() as session, session.begin():
        wall = widgets.wall(session)
    # Ideas joined the floor on 10 Oct.
    assert [w["kind"] for w in wall] == [
        "calendar",
        "ideas",
        "watching",
        "delivered",
        "parked",
        "month",
    ]
    assert all(w["placed"] for w in wall)
    calendar = wall[0]
    assert calendar["size"] == "wide" and calendar["agents_read"] is True


async def test_taken_off_and_kept_the_calendar_is_back_as_it_was(office: Any) -> None:  # noqa: F811
    factory, _, _ = office
    await connected(office)
    with factory() as session, session.begin():
        assert "Client call" in widgets.CATALOG["calendar"].context(session, NOW, "task")  # type: ignore[misc]
        widgets.remove(session, "calendar")
    with factory() as session, session.begin():
        # Off the wall: agents no longer read it, but nothing was deleted.
        assert widgets.reads(session, "calendar") is False
        assert int(session.scalar(select(func.count(CalendarEvent.id))) or 0) == 3
        back = widgets.place(session, "calendar")
    assert back["placed"] and back["agents_read"]
    assert any("synced event" in line for line in back["held"])


async def test_cleaning_the_calendar_out_deletes_what_it_knew(office: Any) -> None:  # noqa: F811
    factory, secrets, _ = office
    source = await connected(office)
    with factory() as session, session.begin():
        save_preferences(session, {"share": "all", "timezone": "Europe/Amsterdam"})
    widgets.clean(widgets.Services(factory, calendars(office)), "calendar")
    with factory() as session, session.begin():
        widgets.remove(session, "calendar")
    with factory() as session:
        assert session.scalar(select(func.count(CalendarSource.id))) == 0
        assert session.scalar(select(func.count(CalendarEvent.id))) == 0
        kept = preferences(session)
        assert kept["share"] == "titles"
        assert kept["timezone"] == "Europe/Amsterdam"
        assert widgets.wall(session)[-1]["held"] == []
    with pytest.raises(SecretStoreError):
        secrets.get(f"calendar-{UUID(source).hex}")


def test_agents_read_only_what_the_owner_switched_on(office: Any) -> None:  # noqa: F811
    factory, _, _ = office
    with factory() as session, session.begin():
        assert widgets.agent_context(session, NOW, "task", skip=("calendar",)) == []
        widgets.change(session, "month", agents_read=True)
        read = widgets.agent_context(session, NOW, "task", skip=("calendar",))
    assert len(read) == 1 and "spending this month" in read[0]
    with factory() as session, session.begin():
        widgets.remove(session, "month")
        assert widgets.agent_context(session, NOW, "task") == []


def test_the_owner_arranges_and_resizes_the_wall(office: Any) -> None:  # noqa: F811
    factory, _, _ = office
    with factory() as session, session.begin():
        order = widgets.arrange(session, ["month", "calendar"])
        assert [w["kind"] for w in order] == [
            "month",
            "calendar",
            "ideas",
            "watching",
            "delivered",
            "parked",
        ]
        assert widgets.change(session, "calendar", size="folded")["size"] == "folded"
        with pytest.raises(widgets.WidgetRejected):
            widgets.change(session, "calendar", size="huge")
        with pytest.raises(widgets.WidgetRejected):
            widgets.place(session, "nothing")
