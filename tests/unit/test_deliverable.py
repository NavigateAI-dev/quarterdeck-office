"""What the owner wants at the end, said before the work starts."""

import pytest

from app.deliverable import DEFAULT, REGISTERED, asked_for, clean, describe


def test_nothing_chosen_falls_back_to_the_default() -> None:
    assert clean(None) == DEFAULT
    assert clean({}) == DEFAULT


def test_a_shape_the_office_does_not_make_is_not_accepted() -> None:
    """A typo must not silently become an instruction to the agents."""
    assert clean({"shape": "novel", "format": "docx"})["shape"] == DEFAULT["shape"]
    assert clean({"shape": "slides", "format": "cuneiform"})["format"] == DEFAULT["format"]


def test_a_shape_keeps_the_format_that_was_asked_for() -> None:
    assert clean({"shape": "comparison", "format": "xlsx"}) == {
        "shape": "comparison",
        "format": "xlsx",
    }


def test_every_shape_suits_a_file_the_office_can_write() -> None:
    from app.deliverable import FORMATS

    assert {shape.suits for shape in REGISTERED.values()} <= set(FORMATS)


def test_the_agents_are_told_the_shape_in_words() -> None:
    told = asked_for({"shape": "comparison", "format": "xlsx"})
    assert "table" in told.lower()
    assert "Excel" in told


def test_on_screen_work_is_not_told_to_produce_a_file() -> None:
    assert "file" not in asked_for({"shape": "report", "format": "onscreen"}).lower()


def test_a_choice_reads_as_a_sentence() -> None:
    assert describe({"shape": "slides", "format": "pptx"}) == "Slides, as PowerPoint"
    assert describe({"shape": "report", "format": "onscreen"}) == "A written report, on screen"


def test_a_new_shape_costs_one_registration() -> None:
    """The point of the registry: slides once took eight edits to add."""
    from app.deliverable import REGISTERED, Shape, offered, register

    try:
        register(
            Shape(
                id="checklist",
                label="A checklist",
                instruction="The owner wants a checklist: what to do, in the order to do it.",
                suits="docx",
                leads=True,
                body="one block per step, each a short heading and what it takes to finish it.",
            )
        )
        # It is offered to the owner, reaches the agents' brief, and leads the
        # document — with no schema change, no renderer edit and no migration.
        assert any(shape["id"] == "checklist" for shape in offered())
        told = asked_for({"shape": "checklist", "format": "docx"})
        assert "checklist" in told and "deliverable_body" in told
        assert "one block per step" in told

        from app.deliverable import leads

        assert leads({"shape": "checklist"}) is True

        from app.documents import document_for

        document = document_for(
            "synthesis",
            {
                "recommendation": "Do it.",
                "facts": ["One."],
                "deliverable_body": [{"heading": "Open the file", "points": ["It is here"]}],
            },
        )
        assert document.sections[0].heading == "Open the file"
    finally:
        REGISTERED.pop("checklist", None)


def test_a_shape_the_office_cannot_hand_over_is_refused_at_registration() -> None:
    from app.deliverable import Shape, register

    with pytest.raises(ValueError, match="cannot write"):
        register(Shape(id="carving", label="A carving", instruction="Carve it.", suits="stone"))


def test_a_shape_that_leads_must_say_how_to_write_its_body() -> None:
    """Otherwise an agent is told to fill a field and not what to put in it."""
    from app.deliverable import Shape, register

    with pytest.raises(ValueError, match="says nothing"):
        register(Shape(id="mute", label="Mute", instruction="Do it.", suits="docx", leads=True))


def test_a_report_is_not_asked_for_a_body_it_does_not_need() -> None:
    """A report's body is its reasoning; a second one would only repeat it."""
    assert "deliverable_body" not in asked_for({"shape": "report", "format": "onscreen"})


def test_earlier_steps_are_not_judged_on_a_deliverable_written_last() -> None:
    """The bug this prevents: a slides task that could never converge.

    Every agent is told what the work should come to, but only the last one
    writes it. The critic, reading at the strategy step, refused the work for
    lacking a deck that cannot exist yet — and kept refusing it, so the
    revision loop stopped as not converging and no deck was ever produced.
    """
    told = asked_for({"shape": "slides", "format": "pptx"})
    assert "written at the final step" in told
    assert "never on whether the deliverable exists yet" in told


def test_a_shape_with_no_body_says_nothing_about_when_it_is_written() -> None:
    """A report has no separate body, so the note would only be noise."""
    assert "final step" not in asked_for({"shape": "report", "format": "docx"})
