"""The lead's prompt and its citation check (Stages 3.4c, 3.4d)."""

from datetime import UTC, datetime

from app.lead.checks import check_citations
from app.lead.prompt import LEAD_PROMPT, Section, lead_messages

NOW = datetime(2026, 10, 16, 14, 52, tzinfo=UTC)


def test_the_stable_part_comes_first_and_does_not_change_with_the_request() -> None:
    one = lead_messages(doctrine="Cite sources.", sections=[], history=[], request="A?", now=NOW)
    two = lead_messages(doctrine="Cite sources.", sections=[], history=[], request="B?", now=NOW)

    assert one[0] == two[0]
    assert one[0]["role"] == "system"
    assert one[0]["content"].startswith(LEAD_PROMPT)
    assert "Cite sources." in one[0]["content"]


def test_runtime_context_is_a_user_message_after_the_stable_part() -> None:
    messages = lead_messages(
        doctrine="",
        sections=[Section("Project", "Pilot for Northwind")],
        history=[],
        request="What next?",
        now=NOW,
    )

    context = messages[1]
    assert context["role"] == "user"
    assert "2026-10-16" in context["content"]
    assert "## Project\nPilot for Northwind" in context["content"]
    assert "Pilot" not in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "What next?"}


def test_earlier_turns_sit_between_the_context_and_the_request() -> None:
    history = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]

    messages = lead_messages(doctrine="", sections=[], history=history, request="Q", now=NOW)

    assert messages[2:4] == history


def test_each_section_is_held_to_its_own_budget() -> None:
    messages = lead_messages(
        doctrine="",
        sections=[Section("Notes", "x" * 5000, max_chars=100)],
        history=[],
        request="Q",
        now=NOW,
    )

    notes = messages[1]["content"].split("## Notes\n", 1)[1]
    assert len(notes) <= 100 + len("\n[cut to fit]")
    assert notes.endswith("[cut to fit]")


def test_the_whole_context_is_held_to_its_budget_dropping_the_last_sections_first() -> None:
    sections = [
        Section("First", "a" * 300),
        Section("Second", "b" * 300),
        Section("Third", "c" * 300),
    ]

    messages = lead_messages(
        doctrine="", sections=sections, history=[], request="Q", now=NOW, context_chars=700
    )

    context = messages[1]["content"]
    assert "## First" in context and "## Second" in context
    assert "## Third" not in context


def test_the_lead_is_told_to_size_the_work_itself() -> None:
    assert "answer directly" in LEAD_PROMPT
    assert "delegate" in LEAD_PROMPT and "propose_team_run" in LEAD_PROMPT


def gathered(*texts: str) -> list[dict[str, str]]:
    return [
        {"role": "tool", "tool_call_id": f"c{n}", "content": text} for n, text in enumerate(texts)
    ]


def test_a_cited_page_that_was_fetched_resolves() -> None:
    check = check_citations(
        "Rain from 15:00 ([KNMI](https://www.knmi.nl/weer)).",
        gathered("- KNMI (https://www.knmi.nl/weer): rain 15:00"),
    )

    assert check.cited == ["https://www.knmi.nl/weer"]
    assert check.unresolved == []
    assert check.ok


def test_a_cited_page_nobody_fetched_does_not_resolve() -> None:
    check = check_citations(
        "See https://made-up.example/report and https://www.knmi.nl/weer.",
        gathered("https://www.knmi.nl/weer"),
    )

    assert check.unresolved == ["https://made-up.example/report"]
    assert not check.ok


def test_trailing_punctuation_and_slashes_do_not_break_a_match() -> None:
    check = check_citations("Source: https://knmi.nl/.", gathered("read https://knmi.nl"))

    assert check.ok


def test_an_answer_citing_nothing_passes() -> None:
    assert check_citations("No rain today.", []).ok
