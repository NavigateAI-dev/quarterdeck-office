"""An answer the owner edited is the answer: on screen, in every file."""

import io

import pytest

from app.documents import Bullets, Document, Paragraph, Section, Sources, Table, document_for
from app.documents.edited import as_dict, from_dict
from app.documents.inline import Run, plain, segments
from app.documents.serialise import body_html

EDITED = Document(
    title="Client onboarding against the Wwft",
    sections=[
        Section(
            heading="Where we fall short",
            blocks=[
                Paragraph("It falls short in **three** places."),
                Bullets(items=["Ongoing monitoring", "Beneficial owners"], ordered=True),
                Table(columns=["Step", "Gap"], rows=[["UBO", "*layered* structures"]]),
            ],
        ),
        Section(heading="Sources", blocks=[Sources(items=["The handbook"])], supporting=True),
    ],
)


def test_a_document_survives_the_round_trip() -> None:
    assert from_dict(as_dict(EDITED)) == EDITED


def test_an_edited_document_is_the_one_shown() -> None:
    payload = {"recommendation": "The agent's words.", "edited_document": as_dict(EDITED)}
    assert document_for("synthesis", payload) == EDITED


def test_a_broken_edited_document_falls_back_to_the_agents_answer() -> None:
    payload = {"recommendation": "The agent's words.", "edited_document": {"sections": "no"}}
    shown = document_for("synthesis", payload)
    page = body_html(shown)
    assert "The agent&#x27;s words." in page
    assert "Edited document" not in page, "a broken edit is not shown as leftovers"


@pytest.mark.parametrize(
    "data",
    [
        {"title": "t", "sections": [{"heading": "h", "blocks": [{"kind": "video"}]}]},
        {"title": "t", "sections": [{"heading": "h", "blocks": [{"kind": "paragraph"}]}]},
        {"title": "t", "sections": [{"heading": 3, "blocks": []}]},
        {"title": "x" * 1000, "sections": []},
        {"title": "t", "sections": [{"heading": "h", "blocks": []}] * 500},
    ],
)
def test_a_malformed_document_is_refused(data: dict) -> None:
    with pytest.raises(ValueError):
        from_dict(data)


def test_bold_and_italic_are_read_from_the_text() -> None:
    assert segments("a **b** *c* d") == [
        Run("a "),
        Run("b", bold=True),
        Run(" "),
        Run("c", italic=True),
        Run(" d"),
    ]
    assert plain("a **b** *c*") == "a b c"


def test_a_lone_star_is_only_a_star() -> None:
    assert segments("5 * 3 and 2*4") == [Run("5 * 3 and 2*4")]


def test_underline_and_links_are_read_from_the_text() -> None:
    """ED1."""
    assert segments("see ++this++ and [the guide](https://example.com/g)") == [
        Run("see "),
        Run("this", underline=True),
        Run(" and "),
        Run("the guide", link="https://example.com/g"),
    ]
    assert plain("[the guide](https://example.com/g)") == "the guide (https://example.com/g)"


def test_only_web_and_mail_links_are_links() -> None:
    assert segments("[x](javascript:alert(1))") == [Run("[x](javascript:alert(1))")]
    assert segments("[mail](mailto:a@b.nl)") == [Run("mail", link="mailto:a@b.nl")]
    assert "href" not in body_html(
        Document(title="t", sections=[Section("h", [Paragraph("[x](javascript:go)")])])
    )


def test_the_page_shows_underline_and_a_safe_link() -> None:
    page = body_html(
        Document(
            title="t", sections=[Section("h", [Paragraph("++u++ [g](https://e.nl/a?b=1&c=2)")])]
        )
    )
    assert "<u>u</u>" in page
    assert '<a href="https://e.nl/a?b=1&amp;c=2" rel="noopener noreferrer">g</a>' in page


def test_markdown_keeps_links_and_writes_underline_as_html() -> None:
    from app.documents import to_markdown

    text = to_markdown(
        Document(title="t", sections=[Section("h", [Paragraph("++u++ [g](https://e.nl)")])])
    )
    assert "<u>u</u> [g](https://e.nl)" in text


def test_a_word_file_carries_underline_and_a_real_hyperlink() -> None:
    from docx import Document as WordDocument

    from app.documents.office_files import to_docx

    data = to_docx(
        Document(title="t", sections=[Section("h", [Paragraph("++u++ [g](https://e.nl)")])])
    )
    word = WordDocument(io.BytesIO(data))
    body = next(p for p in word.paragraphs if "g" in p.text and "u" in p.text)
    assert any(run.text == "u" and run.underline for run in body.runs)
    assert "https://e.nl" in [rel.target_ref for rel in word.part.rels.values() if rel.is_external]


def test_a_slide_carries_a_hyperlink() -> None:
    from pptx import Presentation

    from app.documents.office_files import to_pptx

    deck = Presentation(
        io.BytesIO(
            to_pptx(Document(title="t", sections=[Section("h", [Paragraph("[g](https://e.nl)")])]))
        )
    )
    runs = [
        run
        for slide in deck.slides
        for shape in slide.shapes
        if shape.has_text_frame
        for paragraph in shape.text_frame.paragraphs
        for run in paragraph.runs
    ]
    assert any(run.text == "g" and run.hyperlink.address == "https://e.nl" for run in runs)


def test_the_page_shows_bold_and_italic_and_escapes_the_rest() -> None:
    page = body_html(Document(title="t", sections=[Section("h", [Paragraph("**<b>x</b>** *y*")])]))
    assert "<strong>&lt;b&gt;x&lt;/b&gt;</strong>" in page
    assert "<em>y</em>" in page


def test_a_word_file_carries_bold_and_italic() -> None:
    from docx import Document as WordDocument

    from app.documents.office_files import to_docx

    word = WordDocument(io.BytesIO(to_docx(EDITED)))
    runs = [run for p in word.paragraphs for run in p.runs]
    assert any(run.text == "three" and run.bold for run in runs)
    assert not any("**" in p.text for p in word.paragraphs)
    cell = word.tables[0].rows[1].cells[1].paragraphs[0]
    assert any(run.text == "layered" and run.italic for run in cell.runs)


def test_a_slide_carries_bold_without_its_marks() -> None:
    from pptx import Presentation

    from app.documents.office_files import to_pptx

    deck = Presentation(io.BytesIO(to_pptx(EDITED)))
    texts = [
        run
        for slide in deck.slides
        for shape in slide.shapes
        if shape.has_text_frame
        for paragraph in shape.text_frame.paragraphs
        for run in paragraph.runs
    ]
    assert any(run.text == "three" and run.font.bold for run in texts)
    assert not any("**" in run.text for run in texts)


def test_a_spreadsheet_cell_has_no_marks() -> None:
    from openpyxl import load_workbook

    from app.documents.office_files import to_xlsx

    book = load_workbook(io.BytesIO(to_xlsx(EDITED)))
    values = [cell.value for sheet in book for row in sheet.iter_rows() for cell in row]
    assert "layered structures" in values


def test_a_slide_with_prose_keeps_its_words_whole() -> None:
    """A paragraph was spread one character to a line, so a slide read "I", "t"."""
    from pptx import Presentation

    from app.documents.office_files import to_pptx

    document = Document(title="t", sections=[Section("Why", [Paragraph("It falls short.")])])
    deck = Presentation(io.BytesIO(to_pptx(document)))
    body = deck.slides[1].placeholders[1].text_frame.paragraphs
    assert [p.text for p in body] == ["It falls short."]


def test_text_before_the_first_heading_has_no_empty_heading() -> None:
    from docx import Document as WordDocument

    from app.documents import to_markdown
    from app.documents.office_files import to_docx

    document = Document(title="t", sections=[Section("", [Paragraph("Opening.")])])
    assert "<h2>" not in body_html(document)
    assert "##" not in to_markdown(document)
    word = WordDocument(io.BytesIO(to_docx(document)))
    assert [p.text for p in word.paragraphs if p.style.name == "Heading 1"] == []


def test_speaker_notes_survive_the_round_trip_and_reach_powerpoint() -> None:
    """ED2."""
    from pptx import Presentation

    from app.documents.office_files import to_pptx

    deck = Document(
        title="Quarterly update",
        sections=[
            Section(
                "What changed", [Bullets(items=["Two clients"])], notes="Open with the clients."
            ),
            Section("Next", [Bullets(items=["Approve the step"])]),
        ],
    )
    assert from_dict(as_dict(deck)) == deck
    shown = Presentation(io.BytesIO(to_pptx(deck)))
    notes = [
        slide.notes_slide.notes_text_frame.text for slide in shown.slides if slide.has_notes_slide
    ]
    assert notes == ["Open with the clients."]


def test_notes_are_bounded() -> None:
    data = as_dict(Document(title="t", sections=[Section("h", [], notes="x" * 5000)]))
    with pytest.raises(ValueError):
        from_dict(data)
