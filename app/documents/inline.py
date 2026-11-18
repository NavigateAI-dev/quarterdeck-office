"""Bold, italic, underline and links inside a line of text.

A document's text is plain so every format can write it; the owner's editor
adds the marks a reader most expects, written much as Markdown writes them:
`**bold**`, `*italic*`, `++underline++` and `[label](address)`. A star that
does not open and close a word — `5 * 3`, `2*4` — stays a star. A link is a
link only to a web or mail address; anything else stays the text it was.
"""

from __future__ import annotations

import re
from html import escape
from typing import NamedTuple

# Links first, then bold, so `**x**` is never read as two italics.
MARK = re.compile(
    r"\[(?P<label>[^\[\]\n]+)\]\((?P<link>(?:https?://|mailto:)[^\s()]+)\)"
    r"|\*\*(?P<bold>[^\s*](?:[^*]*?[^\s*])?)\*\*"
    r"|\+\+(?P<underline>[^\s+](?:[^+]*?[^\s+])?)\+\+"
    r"|(?<![*\w])\*(?P<italic>[^\s*](?:[^*]*?[^\s*])?)\*(?![*\w])"
)


class Run(NamedTuple):
    """A stretch of text and its marks."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    link: str | None = None


def segments(text: str) -> list[Run]:
    """The text in runs, marks removed."""
    out: list[Run] = []
    at = 0
    for found in MARK.finditer(text):
        if found.start() > at:
            out.append(Run(text[at : found.start()]))
        if found.group("label") is not None:
            out.append(Run(found.group("label"), link=found.group("link")))
        elif found.group("bold") is not None:
            out.append(Run(found.group("bold"), bold=True))
        elif found.group("underline") is not None:
            out.append(Run(found.group("underline"), underline=True))
        else:
            out.append(Run(found.group("italic"), italic=True))
        at = found.end()
    if at < len(text):
        out.append(Run(text[at:]))
    return out


def plain(text: str) -> str:
    """The text without its marks, a link's address kept beside its label."""
    return "".join(f"{run.text} ({run.link})" if run.link else run.text for run in segments(text))


def to_html(text: str) -> str:
    """Escaped text with its marks as HTML."""
    out: list[str] = []
    for run in segments(text):
        shown = escape(run.text)
        if run.link:
            shown = f'<a href="{escape(run.link)}" rel="noopener noreferrer">{shown}</a>'
        elif run.bold:
            shown = f"<strong>{shown}</strong>"
        elif run.italic:
            shown = f"<em>{shown}</em>"
        elif run.underline:
            shown = f"<u>{shown}</u>"
        out.append(shown)
    return "".join(out)


def to_markdown(text: str) -> str:
    """The text for a Markdown file: underline, which Markdown lacks, as <u>."""
    out: list[str] = []
    for run in segments(text):
        if run.underline:
            out.append(f"<u>{run.text}</u>")
        elif run.link:
            out.append(f"[{run.text}]({run.link})")
        elif run.bold:
            out.append(f"**{run.text}**")
        elif run.italic:
            out.append(f"*{run.text}*")
        else:
            out.append(run.text)
    return "".join(out)
