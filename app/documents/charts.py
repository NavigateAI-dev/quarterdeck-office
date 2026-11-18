"""Charts drawn from a document's own tables.

An agent never draws. It writes its numbers as a table: the first column the
label or the period, the others plain numbers, the unit in the column heading.
Any such table becomes a chart: bars, or a line when the rows are periods
(years, quarters, months, dates). Drawn by the office, twice:

- as SVG inside a page (the office's own view, the web page, the shared page);
- as a PNG for places that show pictures, not pages (Slack, Word).

Every label is model output, so it is escaped in SVG and drawn as text in the
PNG; nothing in a table can become markup or a link.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html import escape

from app.documents.model import Document, Table

MAX_SERIES = 4
MAX_POINTS = 24
MIN_POINTS = 2
PALETTE = ("#2f6f4f", "#d97706", "#3b5b92", "#9b2c1c")
PERIOD = re.compile(
    r"^(?:(?:19|20)\d{2}(?:[-/ ]?(?:Q[1-4]|H[12]|\d{1,2}))?|Q[1-4](?:[ -]?(?:19|20)?\d{2})?|"
    r"H[12](?:[ -]?(?:19|20)?\d{2})?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:[ -]?(?:19|20)?\d{2})?|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|week \d{1,2}|w\d{1,2}|"
    r"\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?|\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
NUMBER = re.compile(r"^[^\d\-+.]*([-+]?\d[\d,  ]*(?:\.\d+)?)\s*([kKmMbB%]|bn|mn)?[^\d]*$")
SCALE = {"k": 1e3, "m": 1e6, "mn": 1e6, "b": 1e9, "bn": 1e9}


@dataclass(frozen=True)
class Chart:
    title: str
    kind: str  # "bar" or "line"
    labels: list[str]
    series: list[tuple[str, list[float]]]


def number(cell: str) -> float | None:
    """A cell as a number: "1,234", "12.5%", "€3.4m", "-7". None if it is not one."""
    text = str(cell).strip()
    if not text or len(text) > 24:
        return None
    found = NUMBER.match(text)
    if not found:
        return None
    digits = found.group(1).replace(",", "").replace(" ", "").replace(" ", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    scale = (found.group(2) or "").lower()
    return value * SCALE.get(scale, 1.0)


def chart_of(table: Table, title: str = "") -> Chart | None:
    """The chart a table makes, or None when it is not a table of numbers."""
    if len(table.columns) < 2 or not MIN_POINTS <= len(table.rows) <= MAX_POINTS:
        return None
    labels = [str(row[0]).strip()[:40] for row in table.rows]
    series: list[tuple[str, list[float]]] = []
    for index, name in enumerate(table.columns[1:], start=1):
        values = [number(row[index]) if index < len(row) else None for row in table.rows]
        if all(value is not None for value in values):
            series.append((str(name).strip()[:40], [float(v) for v in values if v is not None]))
    if not series:
        return None
    periods = sum(1 for label in labels if PERIOD.match(label))
    kind = "line" if len(labels) >= 3 and periods == len(labels) else "bar"
    return Chart(
        title=(title or table.caption or "").strip()[:90],
        kind=kind,
        labels=labels,
        series=series[:MAX_SERIES],
    )


def charts_in(document: Document, answer_only: bool = True) -> list[Chart]:
    """Every chart the document's tables make, in reading order."""
    found = []
    sections = document.answer() if answer_only else document.sections
    for section in sections:
        for block in section.blocks:
            if isinstance(block, Table):
                chart = chart_of(block, section.heading)
                if chart is not None:
                    found.append(chart)
    return found


def table_key(table: Table) -> str:
    """Which table a chart belongs to, as the editor can tell it too."""
    first = str(table.rows[0][0]) if table.rows and table.rows[0] else ""
    return f"{'|'.join(table.columns)}#{len(table.rows)}#{first}"


def chart_svgs(document: Document) -> dict[str, str]:
    """Each table of numbers in the document, drawn, by its key."""
    drawn = {}
    for section in document.sections:
        for block in section.blocks:
            if isinstance(block, Table):
                chart = chart_of(block, section.heading)
                if chart is not None:
                    drawn[table_key(block)] = svg(chart)
    return drawn


def _bounds(chart: Chart) -> tuple[float, float]:
    values = [value for _, points in chart.series for value in points]
    low, high = min(0.0, min(values)), max(0.0, max(values))
    return low, (high if high != low else low + 1.0)


def _short(value: float) -> str:
    size = abs(value)
    for limit, suffix in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if size >= limit:
            return f"{value / limit:.3g}{suffix}"
    return f"{value:.3g}"


# ---- SVG, inside pages ----
WIDTH, HEIGHT = 640, 300
LEFT, RIGHT, TOP, BOTTOM = 56, 16, 28, 64


def svg(chart: Chart) -> str:
    """The chart as an inline SVG, every label escaped."""
    low, high = _bounds(chart)
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def y(value: float) -> float:
        return TOP + plot_h - (value - low) / (high - low) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{escape(chart.title or "Chart")}" xmlns="http://www.w3.org/2000/svg">',
        f'<text x="{LEFT}" y="18" font-size="13" font-weight="600" fill="currentColor">'
        f"{escape(chart.title)}</text>",
    ]
    for step in range(5):
        value = low + (high - low) * step / 4
        at = y(value)
        parts.append(
            f'<line x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{at:.1f}" y2="{at:.1f}" '
            'stroke="currentColor" stroke-opacity="0.15"/>'
            f'<text x="{LEFT - 6}" y="{at + 4:.1f}" font-size="10" text-anchor="end" '
            f'fill="currentColor" fill-opacity="0.7">{escape(_short(value))}</text>'
        )
    count = len(chart.labels)
    slot = plot_w / count
    for index, label in enumerate(chart.labels):
        x = LEFT + slot * (index + 0.5)
        parts.append(
            f'<text x="{x:.1f}" y="{TOP + plot_h + 14}" font-size="10" text-anchor="end" '
            f'transform="rotate(-30 {x:.1f} {TOP + plot_h + 14})" fill="currentColor">'
            f"{escape(label[:18])}</text>"
        )
    for number_, (name, points) in enumerate(chart.series):
        colour = PALETTE[number_ % len(PALETTE)]
        if chart.kind == "line":
            path = " ".join(
                f"{LEFT + slot * (i + 0.5):.1f},{y(value):.1f}" for i, value in enumerate(points)
            )
            parts.append(
                f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="2.5"/>'
            )
        else:
            bar = slot * 0.8 / len(chart.series)
            for i, value in enumerate(points):
                x = LEFT + slot * i + slot * 0.1 + bar * number_
                top, bottom = sorted((y(value), y(0.0)))
                parts.append(
                    f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar:.1f}" '
                    f'height="{max(bottom - top, 0.5):.1f}" fill="{colour}">'
                    f"<title>{escape(name)}: {escape(_short(value))}</title></rect>"
                )
        parts.append(
            f'<rect x="{LEFT + 150 * number_}" y="{HEIGHT - 14}" width="10" height="10" '
            f'fill="{colour}"/><text x="{LEFT + 150 * number_ + 14}" y="{HEIGHT - 5}" '
            f'font-size="10" fill="currentColor">{escape(name[:24])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---- PNG, for Slack and Word ----
def png(chart: Chart, scale: int = 2) -> bytes:
    """The same chart as a PNG, drawn with Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = WIDTH * scale, HEIGHT * scale
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=11 * scale)
    bold = ImageFont.load_default(size=13 * scale)
    low, high = _bounds(chart)
    left, right, top, bottom = LEFT * scale, RIGHT * scale, TOP * scale, BOTTOM * scale
    plot_w, plot_h = width - left - right, height - top - bottom

    def y(value: float) -> float:
        return top + plot_h - (value - low) / (high - low) * plot_h

    draw.text((left, 4 * scale), chart.title, fill="#111111", font=bold)
    for step in range(5):
        value = low + (high - low) * step / 4
        at = y(value)
        draw.line([(left, at), (width - right, at)], fill="#e5e7eb", width=scale)
        draw.text((4 * scale, at - 7 * scale), _short(value), fill="#555555", font=font)
    count = len(chart.labels)
    slot = plot_w / count
    for index, label in enumerate(chart.labels):
        x = left + slot * index + 2 * scale
        draw.text((x, top + plot_h + 4 * scale), label[:12], fill="#333333", font=font)
    for number_, (name, points) in enumerate(chart.series):
        colour = PALETTE[number_ % len(PALETTE)]
        if chart.kind == "line":
            line = [(left + slot * (i + 0.5), y(value)) for i, value in enumerate(points)]
            draw.line(line, fill=colour, width=3 * scale)
        else:
            bar = slot * 0.8 / len(chart.series)
            for i, value in enumerate(points):
                x = left + slot * i + slot * 0.1 + bar * number_
                upper, lower = sorted((y(value), y(0.0)))
                draw.rectangle([x, upper, x + bar, max(lower, upper + 1)], fill=colour)
        key = left + 150 * scale * number_
        draw.rectangle(
            [key, height - 16 * scale, key + 10 * scale, height - 6 * scale], fill=colour
        )
        draw.text((key + 14 * scale, height - 18 * scale), name[:24], fill="#333333", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
