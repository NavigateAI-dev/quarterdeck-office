"""Where an idea sits on the floor: by its day, in the owner's own zone."""

from datetime import date

import pytest

from app.db.models import Idea
from app.ideas import IdeaRejected, bucket, finish

TODAY = date(2026, 10, 10)


@pytest.mark.parametrize(
    ("status", "plan_for", "where"),
    [
        ("open", None, "new"),
        ("open", date(2026, 10, 10), "today"),
        ("open", date(2026, 10, 8), "overdue"),
        ("open", date(2026, 10, 12), "later"),
        ("someday", None, "someday"),
    ],
)
def test_an_idea_sits_by_its_day(status: str, plan_for: date | None, where: str) -> None:
    assert bucket(status, plan_for, TODAY) == where


def test_a_finished_idea_cannot_be_finished_again() -> None:
    idea = Idea(text="Print button", status="done")
    with pytest.raises(IdeaRejected, match="already_done"):
        finish(idea, "dropped")


def test_only_done_or_dropped_finish_an_idea() -> None:
    idea = Idea(text="Print button", status="open")
    with pytest.raises(IdeaRejected, match="unknown_status"):
        finish(idea, "taken")
    finish(idea, "done")
    assert idea.status == "done"
