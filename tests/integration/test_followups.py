"""Following up what was due: gathered, asked about, answered, told in a recap."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.calendars.store import save_preferences
from app.db.models import CalendarEvent, CalendarSource, FollowUp, Idea, RoomMessage
from app.followups import service
from app.followups.tools import follow_up_specs
from app.routines.run import deliver, send_now

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def _event(
    session: Any, uid: str, title: str, starts: datetime, hours: int = 1, **more: Any
) -> Any:
    source = session.scalar(select(CalendarSource).limit(1))
    if source is None:
        source = CalendarSource(kind="ics", provider="other", name="Mine", secret_name="s")
        session.add(source)
        session.flush()
    event = CalendarEvent(
        source_id=source.id,
        uid=uid,
        title=title,
        starts_at=starts,
        ends_at=starts + timedelta(hours=hours),
        **more,
    )
    session.add(event)
    session.flush()
    return event


def _rows(session: Any) -> dict[str, FollowUp]:
    return {row.title: row for row in session.scalars(select(FollowUp))}


def _share(session: Any, share: str) -> None:
    save_preferences(session, {"share": share})
    session.flush()


async def _progress_check(
    client: httpx.AsyncClient, preset: str = service.PRESET
) -> dict[str, Any]:
    made = await client.post(
        "/routines",
        json={
            "title": "Check",
            "prompt": "Ask me.",
            "frequency": "daily",
            "local_time": "16:30",
            "timezone": "UTC",
            "preset": preset,
        },
    )
    assert made.status_code == 201, made.text
    return dict(made.json())


def _reply(session: Any, topic: str, message: int, body: str = "Where does it stand?") -> None:
    session.add(
        RoomMessage(
            topic_id=UUID(topic),
            author_kind="agent",
            author_name="Tim",
            body=body,
            reply_to=message,
        )
    )
    session.flush()


async def test_only_what_is_over_is_gathered_and_once(client: httpx.AsyncClient) -> None:
    with client.app_state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        _event(session, "a", "Pay the tax", NOW - timedelta(hours=3))
        _event(session, "b", "Still to come", NOW + timedelta(hours=3))
        _event(session, "c", "All day errand", NOW - timedelta(hours=10), 24, all_day=True)
        _event(session, "d", "Long ago", NOW - timedelta(days=9))
        service.gather(session, NOW)
        service.gather(session, NOW)
        assert set(_rows(session)) == {"Pay the tax", "All day errand"}


async def test_an_event_moved_or_removed_is_not_asked_about(client: httpx.AsyncClient) -> None:
    with client.app_state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        moved = _event(session, "a", "Moved", NOW - timedelta(hours=3))
        removed = _event(session, "b", "Removed", NOW - timedelta(hours=3))
        service.gather(session, NOW)
        moved.starts_at = NOW + timedelta(days=4)
        moved.ends_at = NOW + timedelta(days=4, hours=1)
        session.delete(removed)
        session.flush()
        service.gather(session, NOW)
        assert service.due_now(session, NOW.date()) == []
        assert _rows(session)["Removed"].status == "gone"


async def test_a_title_said_to_be_no_to_do_is_not_followed_again(
    client: httpx.AsyncClient,
) -> None:
    with client.app_state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        _event(session, "a", "Weekly update", NOW - timedelta(days=1))
        service.gather(session, NOW)
        first = _rows(session)["Weekly update"]
        service.record(session, first, NOW, status="not_a_task")
        _event(session, "b", "Weekly update", NOW - timedelta(hours=2))
        service.gather(session, NOW)
        assert service.due_now(session, NOW.date()) == []
        # The owner wants it asked about after all.
        service.record(session, first, NOW, status="open")
        assert [row.title for row in service.due_now(session, NOW.date())] == ["Weekly update"]


async def test_with_calendar_sharing_on_busy_no_title_reaches_an_agent(
    client: httpx.AsyncClient,
) -> None:
    with client.app_state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        _event(session, "a", "Private", NOW - timedelta(hours=3))
        service.gather(session, NOW)
        _share(session, "busy")
        assert service.due_now(session, NOW.date()) == []
        assert "Private" not in service.recap_words(session, NOW)


async def test_a_planned_idea_is_followed_and_finishing_either_finishes_both(
    client: httpx.AsyncClient,
) -> None:
    with client.app_state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        one = Idea(text="First", status="open", plan_for=NOW.date())
        two = Idea(text="Second", status="open", plan_for=NOW.date())
        later = Idea(text="Later", status="open", plan_for=NOW.date() + timedelta(days=3))
        session.add_all([one, two, later])
        session.flush()
        service.gather(session, NOW)
        rows = _rows(session)
        assert set(rows) == {"First", "Second"}
        service.record(session, rows["First"], NOW, status="done")
        assert one.status == "done"
        two.status = "done"
        service.gather(session, NOW)
        assert rows["Second"].status == "done"


async def test_the_check_asks_nothing_when_nothing_is_due(client: httpx.AsyncClient) -> None:
    routine = await _progress_check(client)
    answer = await client.post(f"/routines/{routine['id']}/send")
    assert answer.status_code == 409
    assert "nothing is due" in answer.text


async def test_the_check_asks_then_stops_asking_after_five_silent_days(
    client: httpx.AsyncClient,
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    routine = await _progress_check(client)
    with factory() as session, session.begin():
        _event(session, "a", "Pay the tax", NOW - timedelta(hours=3))
    routine_id = UUID(routine["id"])
    message = send_now(factory, routine_id, NOW)
    with factory() as session:
        body = session.get(RoomMessage, message).body
    assert "Pay the tax" in body and "not_a_task" in body and "Ask me." in body
    assert "do not ask me anything" not in body
    # Asked twice on one day counts once; five days of silence parks it.
    send_now(factory, routine_id, NOW + timedelta(minutes=5))
    for day in range(1, 5):
        send_now(factory, routine_id, NOW + timedelta(days=day))
    with factory() as session:
        assert _rows(session)["Pay the tax"].asked_count == 5
    with pytest.raises(ValueError, match="nothing is due"):
        send_now(factory, routine_id, NOW + timedelta(days=5))


async def test_a_quiet_check_posts_nothing_and_an_answered_one_says_how_to_reply(
    client: httpx.AsyncClient,
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    routine = await _progress_check(client)
    with factory() as session, session.begin():
        _event(session, "a", "Pay the tax", datetime.now(UTC) - timedelta(hours=3))
    routine_id = UUID(routine["id"])
    message = send_now(factory, routine_id, datetime.now(UTC))
    with factory() as session, session.begin():
        _reply(session, routine["topic_id"], message, "NOTHING TO REPORT")
        assert deliver(session, routine_id, message) == "quiet"


async def test_an_answer_is_recorded_and_a_recap_tells_it_once(client: httpx.AsyncClient) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _event(session, "a", "Pay the tax", now - timedelta(days=1))
        _event(session, "b", "Send the invoice", now - timedelta(days=1))
        service.gather(session, now)
        rows = _rows(session)
        tax, invoice = rows["Pay the tax"].id, rows["Send the invoice"].id
    specs = {spec.name: spec for spec in follow_up_specs(factory, "slack")}
    await specs["follow_up_update"].run({"id": str(tax), "status": "done", "note": "Paid 4750"})
    moved = date.today() + timedelta(days=3)
    await specs["follow_up_update"].run({"id": str(invoice), "next_on": moved.isoformat()})
    with factory() as session:
        row = session.get(FollowUp, tax)
        assert row.status == "done" and row.notes_json[-1]["via"] == "slack"
        assert session.get(FollowUp, invoice).status == "doing"
        assert [r.title for r in service.due_now(session, date.today())] == []

    recap = await _progress_check(client, "evening-recap")
    message = send_now(factory, UUID(recap["id"]), datetime.now(UTC))
    with factory() as session:
        body = session.get(RoomMessage, message).body
    assert "Pay the tax (done: Paid 4750)" in body
    with factory() as session, session.begin():
        _reply(session, recap["topic_id"], message, "Tax is paid.")
        assert deliver(session, UUID(recap["id"]), message) == "delivered"
    next_one = send_now(factory, UUID(recap["id"]), datetime.now(UTC) + timedelta(seconds=2))
    with factory() as session:
        assert "Pay the tax" not in session.get(RoomMessage, next_one).body


async def test_the_page_lists_them_and_the_owner_can_answer(client: httpx.AsyncClient) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        _event(session, "a", "Pay the tax", datetime.now(UTC) - timedelta(days=1))
    seen = (await client.get("/follow-ups")).json()
    assert [t["title"] for t in seen["open"]] == ["Pay the tax"]
    changed = await client.put(
        f"/follow-ups/{seen['open'][0]['id']}", json={"status": "done", "note": "Paid"}
    )
    assert changed.status_code == 200
    seen = (await client.get("/follow-ups")).json()
    assert seen["open"] == [] and seen["finished"][0]["progress"] == "Paid"
    wrong = await client.put(
        f"/follow-ups/{seen['finished'][0]['id']}", json={"status": "nonsense"}
    )
    assert wrong.status_code == 422
