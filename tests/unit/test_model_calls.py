"""Keeping a model call never costs the call."""

from typing import Any
from uuid import uuid4

from app.model_calls import KeptCompleter, digest, request_of
from tests.unit.test_lead_run import Model, said


class Broken:
    def keep(self, *args: Any) -> int:
        raise RuntimeError("database down")


async def test_a_call_that_cannot_be_kept_is_still_answered() -> None:
    kept = KeptCompleter(Model(said("Answer.")), Broken(), uuid4(), "chief")  # type: ignore[arg-type]

    got = await kept.complete("chief", [{"role": "user", "content": "Q"}])

    assert got.content == "Answer."


def test_the_same_request_has_the_same_name_and_bodies_are_named_by_content() -> None:
    messages = [{"role": "user", "content": "Q"}]
    first, bodies = request_of("chief", messages, 100, None, None)
    second, _ = request_of("chief", [dict(messages[0])], 100, None, None)

    assert digest(first) == digest(second)
    assert first["messages"] == [digest(messages[0])] and bodies[digest(messages[0])] == messages[0]
    other, _ = request_of("chief", messages, 200, None, None)
    assert digest(other) != digest(first), "the limits are part of the request"
