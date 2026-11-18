"""Seeing that a task goes in circles, before asking for more money."""

from typing import Any
from uuid import uuid4

from app.orchestrator.dispatcher import looping


class Events:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = steps

    def recent_events(self, run_id: Any, kind: str, limit: int = 12) -> list[dict[str, Any]]:
        assert kind == "browser_step"
        return self.steps[:limit]


def step(label: str, failed: bool) -> dict[str, Any]:
    said = "click 48" + (" (failed: action_timed_out)" if failed else "")
    return {"said": said, "label": label, "url": "https://www.loot-drop.io/"}


def test_the_same_failing_click_reads_as_going_in_circles() -> None:
    steps = [step("click 'Case studies' on www.loot-drop.io", True)] * 9 + [step("scroll", False)]
    found = looping(Events(steps), uuid4())
    assert found is not None
    assert found["what"] == "click 'Case studies' on www.loot-drop.io"
    assert found["tries"] == 9 and found["error"] == "action_timed_out"


def test_a_browser_that_mostly_works_is_not_going_in_circles() -> None:
    steps = [step("a", False)] * 7 + [step("b", True)] * 3
    assert looping(Events(steps), uuid4()) is None


def test_without_a_browser_record_nothing_is_said() -> None:
    assert looping(object(), uuid4()) is None
