"""The same document as a Word file or a spreadsheet.

Both are written from the document model, so what an owner reads on screen and
what they send to someone else are the same content in different clothes. The
libraries are pure Python: python-docx and openpyxl, approved by the owner on
2026-09-21.
"""

from __future__ import annotations

import io
from typing import Any

from docx import Document as WordDocument
from docx.shared import Inches, Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.documents.charts import chart_of, png
from app.documents.inline import plain, segments
from app.documents.model import Bullets, Document, Paragraph, Sources, Table

MAX_COLUMN_WIDTH = 60
# A slide is read at a glance from across a room, not studied.
SLIDE_LINES = 6
SLIDE_CHARS = 140
SHEET_NAME_LIMIT = 31


def to_docx(document: Document) -> bytes:
    """A Word document: headings, prose, lists, and tables as real tables."""
    word = WordDocument()
    word.add_heading(document.title, level=0)
    if document.subtitle:
        note = word.add_paragraph(document.subtitle)
        note.runs[0].italic = True
    for section in document.sections:
        if section.heading:
            word.add_heading(section.heading, level=1)
        for block in section.blocks:
            if isinstance(block, Paragraph):
                _runs(word.add_paragraph(), block.text)
            elif isinstance(block, Bullets):
                style = "List Number" if block.ordered else "List Bullet"
                for item in block.items:
                    _runs(word.add_paragraph(style=style), item)
            elif isinstance(block, Table):
                table = word.add_table(rows=1, cols=len(block.columns))
                table.style = "Table Grid"
                for cell, name in zip(table.rows[0].cells, block.columns, strict=False):
                    run = cell.paragraphs[0].add_run(name)
                    run.bold = True
                for row in block.rows:
                    cells = table.add_row().cells
                    for cell, value in zip(cells, row, strict=False):
                        _runs(cell.paragraphs[0], value)
                if block.caption:
                    caption = word.add_paragraph(block.caption)
                    caption.runs[0].italic = True
                    caption.runs[0].font.size = Pt(9)
                # A table of numbers is drawn as well.
                chart = chart_of(block, section.heading)
                if chart is not None:
                    word.add_picture(io.BytesIO(png(chart)), width=Inches(6))
            elif isinstance(block, Sources):
                for item in block.items:
                    _runs(word.add_paragraph(style="List Number"), item)
    stream = io.BytesIO()
    word.save(stream)
    return stream.getvalue()


def _runs(paragraph: Any, text: str) -> None:
    """Text into a Word paragraph, its marks as runs and links as hyperlinks."""
    for part in segments(text):
        if part.link:
            _word_link(paragraph, part.text, part.link)
            continue
        run = paragraph.add_run()
        run.text = part.text
        run.font.bold = part.bold or None
        run.font.italic = part.italic or None
        run.font.underline = part.underline or None


def _word_link(paragraph: Any, text: str, address: str) -> None:
    """A real hyperlink: python-docx has none, so the element is built."""
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    relation = paragraph.part.relate_to(address, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relation)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(color)
    props.append(underline)
    run.append(props)
    words = OxmlElement("w:t")
    words.set(qn("xml:space"), "preserve")
    words.text = text
    run.append(words)
    link.append(run)
    paragraph._p.append(link)


def _slide_runs(paragraph: Any, text: str) -> None:
    """Text into a PowerPoint paragraph, marks as runs, links on their runs."""
    for part in segments(text):
        run = paragraph.add_run()
        run.text = part.text
        if part.bold:
            run.font.bold = True
        if part.italic:
            run.font.italic = True
        if part.underline:
            run.font.underline = True
        if part.link:
            run.hyperlink.address = part.link


def _sheet_title(name: str, used: set[str]) -> str:
    """Excel refuses a name over 31 characters, and refuses a duplicate."""
    clean = "".join(character for character in name if character not in "[]:*?/\\")
    base = clean[:SHEET_NAME_LIMIT]
    base = base or "Sheet"
    title, suffix = base, 2
    while title in used:
        tail = f" {suffix}"
        title = base[: SHEET_NAME_LIMIT - len(tail)] + tail
        suffix += 1
    used.add(title)
    return title


def _write_row(sheet: Worksheet, values: list[str], bold: bool = False) -> None:
    # A spreadsheet cell holds no marks; the words are what matter there.
    sheet.append([plain(value) for value in values])
    if bold:
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)


def to_xlsx(document: Document) -> bytes:
    """A workbook: one sheet per table, plus the prose on a sheet of its own.

    A comparison the researcher produced as a table is the reason a spreadsheet
    is wanted at all, so tables keep their own sheets rather than being flattened
    into a single column of text.
    """
    book = Workbook()
    blank = book.active
    if blank is not None:
        # A new workbook comes with one empty sheet; the sheets below replace it.
        book.remove(blank)
    used: set[str] = set()

    for section in document.sections:
        for block in section.blocks:
            if not isinstance(block, Table):
                continue
            sheet = book.create_sheet(_sheet_title(section.heading, used))
            _write_row(sheet, block.columns, bold=True)
            for row in block.rows:
                _write_row(sheet, row)
            sheet.freeze_panes = "A2"
            for index, name in enumerate(block.columns, start=1):
                widths = [len(plain(row[index - 1])) for row in block.rows if len(row) >= index]
                longest = max([len(name), *widths])
                sheet.column_dimensions[get_column_letter(index)].width = min(
                    max(12, longest + 2), MAX_COLUMN_WIDTH
                )
                for cell in sheet[get_column_letter(index)]:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)

    notes = book.create_sheet(_sheet_title("Report", used), 0)
    _write_row(notes, [document.title], bold=True)
    if document.subtitle:
        _write_row(notes, [document.subtitle])
    for section in document.sections:
        _write_row(notes, [])
        _write_row(notes, [section.heading], bold=True)
        for block in section.blocks:
            if isinstance(block, Paragraph):
                _write_row(notes, [block.text])
            elif isinstance(block, Bullets | Sources):
                for item in block.items:
                    _write_row(notes, [item])
            elif isinstance(block, Table):
                _write_row(notes, [f"(see the {_sheet_title(section.heading, set())} sheet)"])
    notes.column_dimensions["A"].width = MAX_COLUMN_WIDTH
    for cell in notes["A"]:
        cell.alignment = Alignment(vertical="top", wrap_text=True)

    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def to_pptx(document: Document) -> bytes:
    """The same document as slides: one section to a slide, kept short.

    A deck is not a report with page breaks. Each slide takes a section's
    heading and the lines under it, and long prose is cut rather than shrunk
    to fit, because a slide nobody can read from the back of a room is not a
    slide. The full text is always available in the other formats.
    """
    from pptx import Presentation
    from pptx.util import Pt

    deck = Presentation()
    title_layout, body_layout = deck.slide_layouts[0], deck.slide_layouts[1]

    opening = deck.slides.add_slide(title_layout)
    opening.shapes.title.text = document.title[:120]
    if document.subtitle and len(opening.placeholders) > 1:
        opening.placeholders[1].text = document.subtitle[:200]

    for section in document.sections:
        lines: list[str] = []
        for block in section.blocks:
            if isinstance(block, Paragraph):
                lines.append(_slide_lines(block.text))
            elif isinstance(block, Bullets | Sources):
                lines.extend(_slide_lines(item) for item in block.items)
            elif isinstance(block, Table):
                lines.append(f"({len(block.rows)} rows compared — see the spreadsheet)")
        flat = [line for line in _flatten(lines) if line][:SLIDE_LINES]
        if not flat:
            continue
        slide = deck.slides.add_slide(body_layout)
        slide.shapes.title.text = (section.heading or document.title)[:80]
        frame = slide.placeholders[1].text_frame
        frame.text = ""
        _slide_runs(frame.paragraphs[0], flat[0])
        for line in flat[1:]:
            _slide_runs(frame.add_paragraph(), line)
        for paragraph in frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(16)
        if section.notes:
            slide.notes_slide.notes_text_frame.text = section.notes

    stream = io.BytesIO()
    deck.save(stream)
    return stream.getvalue()


def _slide_lines(text: str) -> str:
    """One line of a slide: what a person can read at a glance."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= SLIDE_CHARS else flat[: SLIDE_CHARS - 1].rstrip() + "…"


def _flatten(lines: list[Any]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if isinstance(line, list):
            out.extend(str(item) for item in line)
        else:
            out.append(str(line))
    return out
