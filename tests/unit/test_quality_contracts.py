"""The reader brief, the plan review, the editor's critique and the answer's
reader-first shape."""

import pytest
from pydantic import ValidationError

from app.schemas.agent_output import Critique, PlanReview, ReaderBrief, Synthesis, TaskPlan
from tests.unit.test_graph import PLAN


def test_a_plan_carries_who_it_is_for() -> None:
    plan = TaskPlan.model_validate(
        PLAN
        | {
            "reader_brief": {
                "reader": "a parent in Lyon choosing an art class for a 5-year-old",
                "purpose": "pick a class and sign up this month",
                "good_enough": ["four to six classes with where, when, price, how to sign up"],
                "voice": "Chinese, plain, friendly",
            }
        }
    )
    assert plan.reader_brief is not None and "parent" in plan.reader_brief.reader


def test_plans_made_before_the_brief_still_load() -> None:
    assert TaskPlan.model_validate(PLAN).reader_brief is None


def test_a_brief_needs_what_good_enough_is() -> None:
    with pytest.raises(ValidationError):
        ReaderBrief.model_validate({"reader": "a parent", "purpose": "choose", "good_enough": []})


def test_a_review_asking_for_change_says_what() -> None:
    with pytest.raises(ValidationError):
        PlanReview.model_validate({"verdict": "change", "changes": []})
    review = PlanReview.model_validate(
        {"verdict": "change", "changes": [{"change": "Search in Dutch", "why": "local sites"}]}
    )
    assert review.changes[0].change == "Search in Dutch"


def test_at_most_three_issues_ranked_for_the_reader() -> None:
    issue = {"issue": "x", "why_it_matters": "y", "fix": "z"}
    with pytest.raises(ValidationError):
        Critique.model_validate({"verdict": "PASS", "confidence": "high", "issues": [issue] * 4})


def test_issues_are_what_a_revision_names() -> None:
    critique = Critique.model_validate(
        {
            "verdict": "REVISE",
            "revision_target": "research",
            "confidence": "medium",
            "issues": [
                {
                    "issue": "Only one class found",
                    "why_it_matters": "the parent cannot compare",
                    "fix": "search kindertekenles Lyon",
                }
            ],
            "for_owner": "Too few options to choose from yet.",
        }
    )
    assert critique.critical_issues == ["Only one class found"]


def test_a_pass_with_writing_fixes_needs_no_revision() -> None:
    critique = Critique.model_validate(
        {
            "verdict": "PASS",
            "confidence": "high",
            "writing_fixes": ["Lead with the three best classes"],
            "for_owner": "Good list; the writing will lead with the best three.",
        }
    )
    assert critique.revision_target is None and critique.critical_issues == []


def test_an_answer_leads_and_says_what_next() -> None:
    answer = Synthesis.model_validate(
        {
            "lead": "Three classes fit: A, B and C.",
            "next_steps": ["Email A for a trial lesson"],
            "not_checked": "Prices for B are not published.",
            "facts": ["A teaches 4-6 year olds"],
            "recommendation": "Start with A.",
        }
    )
    assert answer.lead.startswith("Three classes")
