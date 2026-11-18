"""What a project room says about a finished step.

Written in code from the step's artifact, never by a model: a line generated
per step would cost a call each and could claim work that was not done.
"""

from app.rooms import author_for, summarize


def test_a_plan_says_how_many_questions_and_sources() -> None:
    plan = {
        "research_question": "q",
        "subquestions": ["a", "b", "c", "d"],
        "official_sources": ["https://x", "https://y", "https://z"],
    }
    assert summarize("plan", plan) == "Planned it as 4 questions, with 3 official sources to read."


def test_a_plan_with_one_question_and_no_sources() -> None:
    plan = {"research_question": "q", "subquestions": ["a"], "official_sources": []}
    assert summarize("plan", plan) == "Planned it as 1 question."


def test_research_says_what_it_answered_and_what_refused_it() -> None:
    research = {
        "answers": [
            {"question": "a", "status": "answered", "answer": "x"},
            {"question": "b", "status": "answered", "answer": "x"},
            {"question": "c", "status": "partial", "answer": "x"},
            {"question": "d", "status": "unanswered", "answer": "x"},
        ],
        "blocked_sources": [{"url": "https://dnb.nl", "status": "403"}],
        "sources": [{"url": "https://a"}, {"url": "https://b"}],
    }
    assert summarize("research", research) == ("Answered 2 of 4, 1 in part. 1 source refused us.")


def test_research_without_answers_counts_its_sources() -> None:
    assert summarize("research", {"sources": [{"url": "a"}, {"url": "b"}]}) == "Found 2 sources."


def test_a_critique_that_passes() -> None:
    assert summarize("critique", {"verdict": "PASS", "confidence": "high"}) == "Passed the work."


def test_a_critique_that_sends_work_back_says_what_and_why() -> None:
    critique = {
        "verdict": "REVISE",
        "revision_target": "research",
        "critical_issues": ["Article 6 is quoted but not tied to EMIs."],
        "confidence": "medium",
    }
    assert summarize("critique", critique) == (
        "Sent the research back: Article 6 is quoted but not tied to EMIs."
    )


def test_a_critique_that_blocks() -> None:
    critique = {"verdict": "BLOCK", "critical_issues": ["The premise is invalid."]}
    assert summarize("critique", critique) == "Stopped the work: The premise is invalid."


def test_the_answer_leads_with_the_recommendation() -> None:
    synthesis = {"recommendation": "Apply full CDD before onboarding.", "facts": ["f"]}
    assert (
        summarize("synthesis", synthesis) == "Wrote the answer: Apply full CDD before onboarding."
    )


def test_a_long_line_is_shortened_at_a_word() -> None:
    line = summarize("synthesis", {"recommendation": "word " * 100})
    assert len(line) <= 240 and line.endswith("…")


def test_a_malformed_artifact_still_says_which_step_finished() -> None:
    assert summarize("research", {"answers": "not a list"}) == "Finished the research."
    assert summarize("critique", {}) == "Finished the review."


def test_the_author_is_the_agent_the_run_was_started_with() -> None:
    snapshot = {"agents": [{"profile_id": "p1", "name": "Iris", "role": "researcher"}]}
    assert author_for("research", snapshot) == {
        "kind": "agent",
        "role": "researcher",
        "name": "Iris",
        "profile_id": "p1",
    }


def test_without_a_named_agent_the_role_speaks() -> None:
    assert author_for("plan", {}) == {
        "kind": "agent",
        "role": "planner",
        "name": "Chief",
        "profile_id": None,
    }


def test_a_task_stopped_for_answers_shows_the_chiefs_questions() -> None:
    """E1: the room's waiting card asks the questions, not a code."""
    from app.rooms import owner_questions

    state = {
        "stopped_reason": "needs_owner_answers",
        "plan": {"owner_questions": ["Which six industries?", " ", "Budget?"]},
    }
    assert owner_questions(state) == ["Which six industries?", "Budget?"]
    assert owner_questions({**state, "stopped_reason": "revision_limit_reached"}) == []
    assert owner_questions(None) == []
