"""A task's title: short, stable, and never a surprise."""

from app.titles import TITLE_LIMIT, title_for


def test_a_long_objective_yields_a_short_title() -> None:
    objective = (
        "Research the KYC requirements for a small EMI under the Dutch Wwft, so that we "
        "can decide whether to apply this quarter, and tell me what evidence each "
        "requirement rests on."
    )
    title = title_for(objective)
    assert len(title) <= TITLE_LIMIT + 1, title
    assert title.startswith("Research the KYC requirements")
    assert "so that" not in title


def test_a_title_stops_at_the_first_clause_break() -> None:
    assert title_for("Compare the three providers because we need to pick one") == (
        "Compare the three providers"
    )
    assert title_for("Draft the board update. It should cover the licence gap.") == (
        "Draft the board update"
    )


def test_a_one_line_objective_is_its_own_title() -> None:
    assert title_for("Summarise yesterday's regulator call") == (
        "Summarise yesterday's regulator call"
    )


def test_an_opening_courtesy_is_not_the_title() -> None:
    assert title_for("please can you compare the four KYC vendors") == (
        "Compare the four KYC vendors"
    )
    assert title_for("The goal is to reduce onboarding time") == "Reduce onboarding time"


def test_only_the_first_line_is_the_title() -> None:
    """A pasted brief's first line is the ask; the rest is context."""
    assert title_for("Draft the EMI licence plan\n\nBackground:\n- we hold no licence") == (
        "Draft the EMI licence plan"
    )


def test_a_title_in_a_script_without_spaces_is_still_cut_cleanly() -> None:
    """Chinese has no word boundaries, so cutting at a space would cut nothing."""
    objective = "请评估" + "拟议项目的合规风险" * 12
    title = title_for(objective)
    assert len(title) <= TITLE_LIMIT + 1
    assert title.endswith("…")


def test_an_empty_objective_still_has_a_title() -> None:
    assert title_for("") == "Untitled task"
    assert title_for("   \n  ") == "Untitled task"


def test_a_question_keeps_its_question_mark() -> None:
    """The mark is part of the ask, not punctuation to tidy away."""
    assert title_for("And what would it cost?") == "And what would it cost?"


def test_a_title_the_owner_typed_is_kept_as_typed() -> None:
    from app.titles import title_of

    assert title_of("  EMI due diligence  ", "What does the Wwft require?") == "EMI due diligence"


def test_without_a_typed_title_one_is_derived_from_the_ask() -> None:
    from app.titles import title_for, title_of

    ask = "Please can you research what the Wwft requires, so that we can plan."
    assert title_of(None, ask) == title_for(ask)
    assert title_of("   ", ask) == title_for(ask)
