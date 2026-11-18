"""A document the owner edited, kept as data beside the agent's payload.

The editor sends the document back as plain JSON; it is checked here against
the same model every format is written from, so an edited answer reads,
downloads and is handed to the agents exactly like one an agent wrote. Limits
keep one request from storing something no page could show.
"""

from __future__ import annotations

from typing import Any

from app.documents.model import Block, Bullets, Document, Paragraph, Section, Sources, Table

KEY = "edited_document"
MAX_TITLE = 300
MAX_SECTIONS = 120
MAX_BLOCKS = 400
MAX_TEXT = 20_000
MAX_NOTES = 4_000
MAX_ITEMS = 200
MAX_COLUMNS = 30
MAX_ROWS = 1_000


def as_dict(document: Document) -> dict[str, Any]:
    return {
        "title": document.title,
        "subtitle": document.subtitle,
        "sections": [
            {
                "heading": section.heading,
                "supporting": section.supporting,
                "notes": section.notes,
                "blocks": [_block_dict(block) for block in section.blocks],
            }
            for section in document.sections
        ],
    }


def _block_dict(block: Block) -> dict[str, Any]:
    if isinstance(block, Paragraph):
        return {"kind": "paragraph", "text": block.text}
    if isinstance(block, Bullets):
        return {"kind": "bullets", "items": list(block.items), "ordered": block.ordered}
    if isinstance(block, Table):
        return {
            "kind": "table",
            "columns": list(block.columns),
            "rows": [list(row) for row in block.rows],
            "caption": block.caption,
        }
    return {"kind": "sources", "items": list(block.items)}


def from_dict(data: Any) -> Document:
    """The document, or ValueError saying what is wrong with it."""
    if not isinstance(data, dict):
        raise ValueError("document_not_an_object")
    sections = data.get("sections")
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise ValueError("sections_invalid")
    blocks_seen = 0
    built: list[Section] = []
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("section_invalid")
        blocks = section.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("blocks_invalid")
        blocks_seen += len(blocks)
        if blocks_seen > MAX_BLOCKS:
            raise ValueError("too_many_blocks")
        built.append(
            Section(
                heading=_text(section.get("heading"), MAX_TITLE, empty=True),
                blocks=[_block(block) for block in blocks],
                supporting=bool(section.get("supporting")),
                notes=_text(section.get("notes") or "", MAX_NOTES, empty=True),
            )
        )
    subtitle = data.get("subtitle")
    return Document(
        title=_text(data.get("title"), MAX_TITLE),
        subtitle=_text(subtitle, MAX_TITLE) if subtitle is not None else None,
        sections=built,
    )


def _block(data: Any) -> Block:
    if not isinstance(data, dict):
        raise ValueError("block_invalid")
    kind = data.get("kind")
    if kind == "paragraph":
        return Paragraph(_text(data.get("text"), MAX_TEXT, empty=True))
    if kind == "bullets":
        return Bullets(items=_texts(data.get("items")), ordered=bool(data.get("ordered")))
    if kind == "sources":
        return Sources(items=_texts(data.get("items")))
    if kind == "table":
        columns = _texts(data.get("columns"), MAX_COLUMNS)
        rows = data.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError("rows_invalid")
        caption = data.get("caption")
        return Table(
            columns=columns,
            rows=[_texts(row, MAX_COLUMNS) for row in rows],
            caption=_text(caption, MAX_TITLE) if caption else None,
        )
    raise ValueError(f"unknown_block:{str(kind)[:20]}")


def _text(value: Any, limit: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("text_invalid")
    return value


def _texts(value: Any, limit: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("list_invalid")
    return [_text(item, MAX_TEXT, empty=True) for item in value]
