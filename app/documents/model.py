"""What a finished task produces, before it is any particular file format.

An agent's output is structured: a research summary has findings, a comparison
table with a source per row, limits and a recommendation. That structure is
good material; what was missing was anything that turned it into a document a
person reads. This is that middle step — one model, rendered once per artifact
type, then written out as Markdown, HTML, Word or a spreadsheet.

Deliberately plain: no styling, no format-specific fields. A serialiser decides
how a heading looks; this only says that it is a heading.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class Bullets:
    items: list[str]
    # A numbered list where order carries meaning, such as steps.
    ordered: bool = False


@dataclass(frozen=True)
class Table:
    columns: list[str]
    rows: list[list[str]]
    caption: str | None = None


@dataclass(frozen=True)
class Sources:
    """Where the claims came from, kept apart so every format can footnote it."""

    items: list[str]


Block = Paragraph | Bullets | Table | Sources


@dataclass(frozen=True)
class Section:
    heading: str
    blocks: list[Block] = field(default_factory=list)
    # Supporting sections are the working, not the answer: how far the task
    # got, what it assumed, what it could not settle. They are kept, and kept
    # out of the way, because a reader looking for the answer should not have
    # to walk through the engineering to find it.
    supporting: bool = False
    # What a presenter says over this section when it is a slide.
    notes: str = ""


@dataclass(frozen=True)
class Document:
    title: str
    subtitle: str | None = None
    sections: list[Section] = field(default_factory=list)

    def answer(self) -> list[Section]:
        """What the task was asked for."""
        return [section for section in self.sections if not section.supporting]

    def working(self) -> list[Section]:
        """How it got there, and what it could not settle."""
        return [section for section in self.sections if section.supporting]

    def tables(self) -> list[Table]:
        """Every table in the document, for formats that hold only tables."""
        return [
            block
            for section in self.sections
            for block in section.blocks
            if isinstance(block, Table)
        ]
