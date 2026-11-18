"""A room fed by a routine can tell each run apart from what the owner typed."""

from datetime import UTC, datetime

from app.db.models import RoomMessage
from app.rooms import _said


def _message(client_key: str | None) -> RoomMessage:
    return RoomMessage(
        id=7,
        author_kind="office",
        author_name="The office",
        body="Morning recap (a routine, not typed by me): ...",
        client_key=client_key,
        created_at=datetime(2026, 11, 5, 7, 30, tzinfo=UTC),
    )


def test_a_message_a_routine_asked_is_marked_as_a_run() -> None:
    assert _said(_message("routine:abc:1762327800"))["routine"] is True


def test_a_message_the_owner_typed_is_not_a_run() -> None:
    assert _said(_message("web:123"))["routine"] is False


def test_a_message_without_a_request_id_is_not_a_run() -> None:
    assert _said(_message(None))["routine"] is False
