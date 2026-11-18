"""Write a document as Markdown or HTML.

Both are dependency-free. The HTML is also what the office shows on screen and
what a browser prints to PDF, so it carries its own small stylesheet rather
than relying on the office's.
"""

from __future__ import annotations

from html import escape

from app.documents.charts import chart_of, svg
from app.documents.inline import to_html as inline_html
from app.documents.inline import to_markdown as inline_markdown
from app.documents.model import Bullets, Document, Paragraph, Section, Sources, Table

STYLE = """
:root { color-scheme: light dark; }
body { margin: 0 auto; max-width: 46rem; padding: 2rem 1.25rem 4rem;
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 1.75rem; margin-bottom: .25rem; }
.subtitle { color: #6b7280; margin-top: 0; }
h2 { font-size: 1.15rem; margin-top: 2rem; padding-bottom: .3rem;
  border-bottom: 1px solid #e5e7eb; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .92rem; }
th, td { border: 1px solid #e5e7eb; padding: .5rem .6rem; text-align: left; vertical-align: top; }
th { background: #f9fafb; }
caption { caption-side: bottom; color: #6b7280; font-size: .85rem; padding-top: .5rem; }
ol.sources { font-size: .9rem; color: #374151; }
ol.sources li { margin-bottom: .35rem; word-break: break-word; }
.chart-figure { margin: .5rem 0 1.5rem; }
svg.chart { width: 100%; max-width: 640px; height: auto; display: block; }
@media print { body { max-width: none; }
  h2 { break-after: avoid; } table { break-inside: avoid; } }
"""


def to_markdown(document: Document) -> str:
    lines: list[str] = [f"# {document.title}", ""]
    if document.subtitle:
        lines += [f"*{document.subtitle}*", ""]
    working = document.working()
    for index, section in enumerate([*document.answer(), *working]):
        if working and index == len(document.answer()):
            lines += ["---", "", "## How it got there", ""]
        if section.heading:
            lines += [f"{'###' if section.supporting else '##'} {section.heading}", ""]
        for block in section.blocks:
            if isinstance(block, Paragraph):
                lines += [inline_markdown(block.text), ""]
            elif isinstance(block, Bullets):
                for index, item in enumerate(block.items, start=1):
                    item = inline_markdown(item)
                    lines.append(f"{index}. {item}" if block.ordered else f"- {item}")
                lines.append("")
            elif isinstance(block, Table):
                lines.append("| " + " | ".join(block.columns) + " |")
                lines.append("| " + " | ".join("---" for _ in block.columns) + " |")
                for row in block.rows:
                    # A pipe inside a cell would end the column early.
                    cells = (inline_markdown(cell).replace("|", "\\|") for cell in row)
                    lines.append("| " + " | ".join(cells) + " |")
                if block.caption:
                    lines += ["", f"*{block.caption}*"]
                lines.append("")
            elif isinstance(block, Sources):
                for index, item in enumerate(block.items, start=1):
                    lines.append(f"{index}. {item}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_html(document: Document) -> str:
    """A standalone page. Every value is escaped: payloads are model output."""
    out: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(document.title)}</title>",
        f"<style>{STYLE}</style></head><body>",
        f"<h1>{escape(document.title)}</h1>",
    ]
    if document.subtitle:
        out.append(f'<p class="subtitle">{escape(document.subtitle)}</p>')
    out.append(body_html(document))
    out.append("</body></html>")
    return "\n".join(out)


def body_html(document: Document) -> str:
    """The answer, then the working behind a fold.

    A task's result used to open with its own engineering log; the answer was
    in the middle of it. The working is still here, one click away.
    """
    out: list[str] = [_sections_html(document.answer())]
    working = document.working()
    if working:
        out.append(
            '<details class="working"><summary>How it got there — '
            f"{len(working)} more section{'s' if len(working) != 1 else ''}</summary>"
            + _sections_html(working)
            + "</details>"
        )
    return "\n".join(part for part in out if part)


def _sections_html(sections: list[Section]) -> str:
    out: list[str] = []
    for section in sections:
        # Text before the first heading belongs to no heading.
        if section.heading:
            out.append(f"<h2>{escape(section.heading)}</h2>")
        for block in section.blocks:
            if isinstance(block, Paragraph):
                out.append(f"<p>{inline_html(block.text)}</p>")
            elif isinstance(block, Bullets):
                tag = "ol" if block.ordered else "ul"
                items = "".join(f"<li>{inline_html(item)}</li>" for item in block.items)
                out.append(f"<{tag}>{items}</{tag}>")
            elif isinstance(block, Table):
                head = "".join(f"<th>{escape(name)}</th>" for name in block.columns)
                rows = "".join(
                    "<tr>" + "".join(f"<td>{inline_html(cell)}</td>" for cell in row) + "</tr>"
                    for row in block.rows
                )
                caption = f"<caption>{escape(block.caption)}</caption>" if block.caption else ""
                # The frame scrolls sideways, so a wide table stays in its column.
                out.append(
                    f'<div class="table-frame"><table>{caption}<thead><tr>{head}</tr></thead>'
                    f"<tbody>{rows}</tbody></table></div>"
                )
                # A table of numbers is drawn as well.
                chart = chart_of(block, section.heading)
                if chart is not None:
                    out.append(f'<figure class="chart-figure">{svg(chart)}</figure>')
            elif isinstance(block, Sources):
                items = "".join(f"<li>{escape(item)}</li>" for item in block.items)
                out.append(f'<ol class="sources">{items}</ol>')
    return "\n".join(out)
