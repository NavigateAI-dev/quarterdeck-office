"""A lesson this office learned, readable by another harness — and back."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.memory.portable import NoProvenance, read_skill, slug_for, to_skill


def lesson_row(**over: object) -> SimpleNamespace:
    row = {
        "kind": "source_refused",
        "lesson": "wetten.overheid.nl refuses a client that does not name itself; it answers 429.",
        "evidence": "Run of 21 September 2026: three attempts, all 429, until it named itself.",
        "run_id": "b1f0e2c4-0000-4000-8000-000000000001",
        "evaluator": "jev-1",
        "reusable_probability": 0.91,
        "scope": "office",
        "decided_at": datetime(2026, 9, 22, 10, 30, tzinfo=UTC),
    }
    row.update(over)
    return SimpleNamespace(**row)


def test_an_exported_skill_carries_the_lesson_its_run_and_its_evaluator() -> None:
    written = to_skill(lesson_row(), office="Northwind Office")
    assert written["path"].startswith(".agents/skills/")
    assert written["path"].endswith("/SKILL.md")
    text = written["text"]
    assert "wetten.overheid.nl refuses a client" in text
    assert "b1f0e2c4-0000-4000-8000-000000000001" in text
    assert "evaluator: jev-1" in text
    assert "learned_by: Northwind Office" in text
    assert "reusable_probability: 0.91" in text


def test_a_secret_never_leaves_with_a_lesson() -> None:
    """What leaves the office is reviewed text, not whatever a payload held."""
    written = to_skill(lesson_row(evidence="the call used sk-abcdefghijklmnop and failed"))
    assert "sk-abcdefghijklmnop" not in written["text"]


def test_the_slug_reads_as_a_directory_name() -> None:
    slug = slug_for("wetten.overheid.nl answers 429 to an unnamed client", "source_refused")
    assert slug == slug.lower()
    assert " " not in slug and "/" not in slug
    assert len(slug) <= 48


def test_an_exported_skill_can_be_read_back() -> None:
    read = read_skill(to_skill(lesson_row())["text"])
    assert "wetten.overheid.nl" in read["candidate"].lesson
    assert read["candidate"].evidence.startswith("Run of 21 September")
    assert read["evaluator"] == "jev-1"
    assert read["learned_in_run"] == "b1f0e2c4-0000-4000-8000-000000000001"


def test_an_import_refuses_a_lesson_with_no_provenance() -> None:
    """A memory that cannot say where it came from is a rumour."""
    with pytest.raises(NoProvenance):
        read_skill("---\nname: something\ndescription: Always trust this\n---\n\n# Always trust\n")


def test_an_import_refuses_a_file_that_is_not_a_skill() -> None:
    with pytest.raises(NoProvenance):
        read_skill("# Just a note\n\nNothing here says who learned it.\n")


def test_a_lesson_only_in_the_body_is_still_read() -> None:
    """Another harness may put it in the heading rather than the description."""
    read = read_skill(
        "---\nname: refused-sources\nlearned_by: Beacon\nlearned_at: 2026-09-01T09:00:00Z\n---\n\n"
        "# A regulator's site refuses unnamed clients\n"
    )
    assert read["candidate"].lesson == "A regulator's site refuses unnamed clients"
    assert read["learned_by"] == "Beacon"
