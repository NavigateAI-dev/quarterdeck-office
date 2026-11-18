"""The owner's calendars against the database."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.calendars.store import (
    SENT_GONE,
    CalendarRejected,
    Calendars,
    agent_view,
    events_between,
    owner_status,
    preferences,
    save_preferences,
)
from app.db.models import CalendarChange, CalendarEvent, CalendarSource
from app.db.session import make_engine, make_session_factory
from app.secret_store import SecretStore, generate_root_key
from tests.integration.api_support import database_url

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 7, 8, 20, tzinfo=UTC)


def ics(*events: tuple[str, str, str, str]) -> str:
    body = "".join(
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
        "DESCRIPTION:Secret dial-in 4242\r\nLOCATION:Room 3\r\n"
        "ATTENDEE:mailto:a@example.com\r\nATTENDEE:mailto:b@example.com\r\nEND:VEVENT\r\n"
        for uid, title, start, end in events
    )
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n{body}END:VCALENDAR\r\n"


DAY = ics(
    ("call", "Client call", "20261007T080000Z", "20261007T090000Z"),
    ("sync", "Team sync", "20261007T090000Z", "20261007T093000Z"),
    ("later", "Board prep", "20261007T140000Z", "20261007T150000Z"),
)


class Server:
    """A calendar link that answers what the test says, and counts its readers."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.status = 200
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return httpx.Response(self.status, text=self.text)


@pytest.fixture
def office(tmp_path: Path) -> Iterator[tuple[Any, SecretStore, Server]]:
    url = database_url()
    schema = f"test_cal_{uuid4().hex}"
    engine = make_engine(url)
    quoted = engine.dialect.identifier_preparer.quote(schema)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {quoted}"))
        connection.execute(text(f"SET search_path TO {quoted}"))
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    engine.dispose()
    scoped = make_engine(f"{url}?options=-csearch_path%3D{schema}")
    factory = make_session_factory(scoped)
    generate_root_key(tmp_path / "root.key")
    secrets = SecretStore(tmp_path / "secrets.json", (tmp_path / "root.key").read_bytes())
    server = Server(DAY)
    yield factory, secrets, server
    scoped.dispose()
    cleanup = make_engine(url)
    with cleanup.connect() as connection:
        connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
        connection.commit()
    cleanup.dispose()


def calendars(office: Any) -> Calendars:
    factory, secrets, server = office
    return Calendars(
        factory,
        lambda: secrets,
        http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(server)),
        clock=lambda: NOW,
    )


async def test_a_calendar_link_is_read_and_its_address_kept_sealed(office: Any) -> None:
    factory, secrets, server = office
    added = await calendars(office).connect(
        "ics", "google", "Work", url="https://calendar.example.com/private-abc/basic.ics"
    )
    assert added["event_count"] == 3 and added["last_error"] is None
    assert "url" not in added and "private-abc" not in str(added)
    assert secrets.get(f"calendar-{UUID(added['id']).hex}").endswith("basic.ics")
    with factory() as session:
        titles = [e.title for e in events_between(session, NOW, NOW + timedelta(days=1))]
    assert titles == ["Client call", "Team sync", "Board prep"]


async def test_a_dead_link_is_refused_and_nothing_is_kept(office: Any) -> None:
    factory, secrets, server = office
    server.status = 404
    with pytest.raises(CalendarRejected) as raised:
        await calendars(office).connect("ics", "outlook", "Work", url="https://x.example/c.ics")
    assert raised.value.code == "calendar_not_found"
    assert calendars(office).sources() == [] and secrets.names("calendar-") == []


async def test_a_sync_follows_the_calendar_and_a_failure_keeps_the_last_day(
    office: Any,
) -> None:
    factory, secrets, server = office
    store = calendars(office)
    added = await store.connect("ics", "google", "Work", url="https://c.example/a.ics")
    server.text = ics(("call", "Client call (moved)", "20261007T100000Z", "20261007T110000Z"))
    synced = await store.sync(UUID(added["id"]))
    assert synced is not None and synced["event_count"] == 1
    server.status = 401
    failed = await store.sync(UUID(added["id"]))
    assert failed is not None and failed["last_error"] == "calendar_refused"
    with factory() as session:
        titles = [e.title for e in session.scalars(select(CalendarEvent))]
    assert titles == ["Client call (moved)"], "a failed sync keeps what was read last"


async def test_disconnecting_removes_the_events_and_the_secret(office: Any) -> None:
    factory, secrets, server = office
    store = calendars(office)
    added = await store.connect("ics", "google", "Work", url="https://c.example/a.ics")
    assert store.disconnect(UUID(added["id"])) is True
    with factory() as session:
        assert session.scalars(select(CalendarEvent)).first() is None
    assert secrets.names("calendar-") == []


async def test_the_owner_is_busy_until_back_to_back_meetings_end(office: Any) -> None:
    factory, _, _ = office
    await calendars(office).connect("ics", "google", "Work", url="https://c.example/a.ics")
    with factory() as session:
        status = owner_status(session, NOW)
        later = owner_status(session, NOW + timedelta(hours=2))
    assert status["state"] == "busy" and status["title"] == "Client call"
    assert status["until"] == "2026-10-07T09:30:00+00:00", "the sync straight after counts"
    assert later["state"] == "free" and later["next"]["title"] == "Board prep"


async def test_agents_see_titles_and_times_but_not_the_details(office: Any) -> None:
    factory, _, _ = office
    await calendars(office).connect("ics", "google", "Work", url="https://c.example/a.ics")
    with factory() as session:
        assert preferences(session)["share"] == "titles", "the owner's choice on 7 Oct"
        seen = agent_view(session, NOW)
        assert "Board prep" in seen and "4242" not in seen and "Room 3" not in seen
        save_preferences(session, {"share": "busy"})
        seen = agent_view(session, NOW)
        assert "Board prep" not in seen and "busy" in seen
        save_preferences(session, {"share": "all"})
        assert "4242" in agent_view(session, NOW)


async def test_with_no_calendar_agents_are_told_nothing(office: Any) -> None:
    factory, _, _ = office
    with factory() as session:
        assert agent_view(session, NOW) == ""


async def test_notifications_wait_while_the_owner_is_in_a_meeting(office: Any) -> None:
    """CAL3: held while busy, if the owner chose it; never held otherwise."""
    from app.calendars.store import holding

    factory, _, _ = office
    await calendars(office).connect("ics", "google", "Work", url="https://c.example/a.ics")
    with factory() as session, session.begin():
        assert holding(session, NOW) is True, "in the client call"
        assert holding(session, NOW + timedelta(hours=2)) is False, "free again"
        save_preferences(session, {"hold": False})
        assert holding(session, NOW) is False, "the owner turned it off"


async def test_a_change_sent_with_no_answer_is_unknown_not_failed(office: Any) -> None:
    """Stage 5c: the calendar may have made it; the owner checks, and it is not sent again."""
    factory, _, _ = office
    with factory() as session, session.begin():
        source = CalendarSource(kind="google", provider="google", name="Work", secret_name="s")
        session.add(source)
        session.flush()
        change = CalendarChange(source_id=source.id, action="create", payload_json={})
        session.add(change)
        session.flush()
        change_id = change.id

    said = calendars(office).change_applied(change_id, False, "calendar_no_answer", unknown=True)

    assert said is not None and said["status"] == "unknown"


def mac_change(office: Any) -> int:
    factory, _, _ = office
    with factory() as session, session.begin():
        source = CalendarSource(kind="mac", provider="mac", name="This Mac", secret_name="mac")
        session.add(source)
        session.flush()
        change = CalendarChange(source_id=source.id, action="create", payload_json={})
        session.add(change)
        session.flush()
        return change.id


async def test_a_change_handed_to_the_mac_helper_is_not_handed_out_again(office: Any) -> None:
    """The helper may make it and stop before it says so: a second hand-out would
    make the same event twice."""
    change_id = mac_change(office)
    mac = calendars(office)

    assert [c["id"] for c in mac.pending_changes()] == [change_id]
    assert mac.pending_changes() == []
    assert (mac.change(change_id) or {})["status"] == "sent"


async def test_a_change_the_helper_never_reports_on_becomes_unknown(office: Any) -> None:
    factory, secrets, server = office
    now = [NOW]
    mac = Calendars(factory, lambda: secrets, clock=lambda: now[0])
    change_id = mac_change(office)
    mac.pending_changes()

    now[0] = NOW + SENT_GONE - timedelta(seconds=1)
    assert (mac.change(change_id) or {})["status"] == "sent", "still within its time"
    now[0] = NOW + SENT_GONE + timedelta(seconds=1)
    assert (mac.change(change_id) or {})["status"] == "unknown"
    assert mac.pending_changes() == [], "not sent again: the owner checks"
    late = mac.change_applied(change_id, True)
    assert late is not None and late["status"] == "done", "a late report still settles it"
