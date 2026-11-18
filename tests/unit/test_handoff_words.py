"""handoff: reading "later" and explaining a failed start (characterisation).

The chat paths merge into one ChatService in S3; these pin the owner-facing
wording rules that must carry over unchanged.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.handoff import explain, returns_on

# A Tuesday.
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("said", "days"),
    [
        ("come back to this in 3 days", 3),
        ("in 2 weeks please", 14),
        ("in 1 month", 30),
        ("tomorrow", 1),
        ("next week", 7),
        ("next month", 30),
        ("Friday", 3),
        ("on tuesday", 7),  # the same weekday means next week, never today
        ("later", 7),  # nothing recognised: a week
    ],
)
def test_returns_on_reads_the_owners_words(said: str, days: int) -> None:
    assert returns_on(said, NOW) == NOW + timedelta(days=days)


def test_explicit_count_wins_over_a_weekday() -> None:
    assert returns_on("in 2 days, not friday", NOW) == NOW + timedelta(days=2)


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("OfficePaused", "new work is paused in Settings."),
        ("MonthlyLimitReached", "this month's spending limit is reached."),
        ("UnknownProject", "its project no longer exists."),
        ("Anything", "the office could not start it."),
    ],
)
def test_explain_says_why_work_did_not_start(name: str, words: str) -> None:
    error = type(name, (Exception,), {})()
    assert explain(error) == words
