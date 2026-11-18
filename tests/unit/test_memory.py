"""Turning a finished run into project memory.

Modelled on Beacon's cross-harness memory: candidates come from what the run
recorded, Jev ranks them, and only the owner approves.
"""

import pytest

from app.memory.candidates import Candidate, candidates_for, redact
from app.memory.evaluate import NothingLearned, evaluate, projection

REPORT = {
    "answers": [
        {
            "question": "Which vendors support Dutch eID?",
            "status": "unanswered",
            "answer": "Only marketing pages were retrieved.",
            "missing": ["Vendor documentation naming the schemes"],
        },
        {"question": "What does the Wwft require?", "status": "answered", "answer": "Article 3."},
    ],
    "blocked_sources": [
        {"url": "https://www.dnb.nl/en/", "status": "http_403"},
        {"url": "https://wetten.overheid.nl/BWBR0001854/", "status": "not_the_document_named"},
    ],
}
CRITIQUE = {"critical_issues": ["The vendor comparison lists 2 of the 5 required"]}


def test_the_lessons_of_the_kyc_run_are_recovered() -> None:
    """These were learned three times over and thrown away three times."""
    found = candidates_for(REPORT, CRITIQUE)
    kinds = {c.kind for c in found}
    assert {"source_blocked", "wrong_address", "unanswered", "review_issue"} <= kinds
    blocked = next(c for c in found if c.kind == "source_blocked")
    assert "dnb.nl" in blocked.lesson and "http_403" in blocked.lesson
    wrong = next(c for c in found if c.kind == "wrong_address")
    assert "BWBR0001854" in wrong.lesson
    # An answered question teaches nothing.
    assert not any("What does the Wwft require?" in c.lesson for c in found)


def test_a_budget_stop_becomes_a_lesson_about_the_next_task() -> None:
    usage = {"spent": {"tokens": 397284}, "budget": {"max_tokens": 400000}}
    found = candidates_for({}, None, usage, stopped="budget")
    assert found and "397,284" in found[0].lesson
    assert "400,000" in found[0].lesson


def test_a_credential_never_travels() -> None:
    dirty = {
        "blocked_sources": [
            {"url": "https://api.example/?key=sk-abcdef0123456789", "status": "http_401"}
        ]
    }
    found = candidates_for(dirty)
    assert "sk-abcdef0123456789" not in found[0].lesson
    assert "[redacted]" in found[0].lesson
    assert redact("Bearer abcdef0123456789") == "[redacted]"


def test_the_same_lesson_is_not_learned_twice_in_one_run() -> None:
    twice = {"blocked_sources": [{"url": "https://a.example", "status": "http_403"}] * 3}
    assert len(candidates_for(twice)) == 1


async def test_a_dry_run_sends_nothing() -> None:
    """The only way to be sure what leaves the machine is to send nothing."""

    class Refuses:
        async def reusable(self, lessons: list[str], project: str) -> list[float]:
            raise AssertionError("a dry run must not call the evaluator")

    found, probabilities, evaluator = await evaluate(
        REPORT, CRITIQUE, None, "", "Project", Refuses(), dry_run=True
    )
    assert found and probabilities == [] and evaluator is None


async def test_what_would_be_sent_is_only_lessons_and_their_evidence() -> None:
    found = candidates_for(REPORT, CRITIQUE)
    sent = projection(found)
    assert all(set(item) == {"kind", "lesson", "evidence"} for item in sent)
    flat = str(sent)
    assert "prompt" not in flat and "artifact" not in flat


async def test_jev_ranks_but_does_not_decide() -> None:
    class Ranks:
        async def reusable(self, lessons: list[str], project: str) -> list[float]:
            return [0.9] * len(lessons)

    found, probabilities, evaluator = await evaluate(REPORT, CRITIQUE, None, "", "Project", Ranks())
    assert len(probabilities) == len(found)
    assert evaluator == "jev-latest"
    # Nothing in the candidate itself says approved: that is the owner's word.
    assert all(isinstance(c, Candidate) for c in found)


async def test_a_run_that_learned_nothing_says_so() -> None:
    with pytest.raises(NothingLearned):
        await evaluate({}, {}, None, "", "Project", None)


async def test_an_evaluator_that_is_unavailable_still_yields_candidates() -> None:
    from app.judgments import JudgmentUnavailable

    class Down:
        async def reusable(self, lessons: list[str], project: str) -> list[float]:
            raise JudgmentUnavailable("offline")

    found, probabilities, evaluator = await evaluate(REPORT, CRITIQUE, None, "", "Project", Down())
    assert found and probabilities == [] and evaluator is None
