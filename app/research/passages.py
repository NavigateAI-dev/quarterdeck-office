"""Read a long source at its sections, rather than its first pages.

A law, a regulation or a standard is not written to be read from the top for
twelve thousand characters. That prefix is navigation, a table of contents and
a preamble; the obligations are further down. The office used to take that
prefix and then report, accurately, that the source did not contain what was
asked — which is how a research task ends with its own question listed as an
unknown.

So: keep the whole document, drop the navigation, split it at its own headings,
and spend the character budget on the sections that bear on the question.
Nothing here calls a model: selection is by the terms a question and a document
share, which across languages are the names, codes and numbers that matter
(Wwft, eIDAS, DNB, UBO, "Artikel 3").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Headings a legal or regulatory document uses for its own divisions.
HEADING = re.compile(
    r"(?m)^\s*("
    r"Artikel\s+\d+[a-z]*|Article\s+\d+[a-z]*|"
    r"Hoofdstuk\s+[\dIVXLC]+|Chapter\s+[\dIVXLC]+|"
    r"Afdeling\s+[\dIVXLC]+|Section\s+[\dIVXLC]+|"
    r"Paragraaf\s+[\dIVXLC]+|Annex\s+[A-Z\d]+|Bijlage\s+[A-Z\d]+|"
    r"§\s*\d+[a-z]*"
    r")\b[^\n]{0,120}$"
)
# Words that mark the page's furniture rather than its content.
FURNITURE = (
    "direct naar content",
    "primaire navigatie",
    "skip to main content",
    "cookie",
    "deze website gebruikt",
    "menu",
    "zoeken",
    "inloggen",
    "search",
    # Per-article furniture on the Dutch legislation site.
    "toon relaties in lido",
    "maak een permanente link",
    "toon wetstechnische informatie",
    "vergelijk met een eerdere versie",
    "druk het regelingelement af",
    "sla het regelingelement op",
)
# A term worth matching on: a name, a code, an abbreviation or a number. These
# survive translation, which a question in one language and a law in another do
# not otherwise share.
TERM = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9./-]{2,}|\d{1,4}")
COMMON = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "how",
        "which",
        "what",
        "van",
        "de",
        "het",
        "een",
        "met",
        "voor",
        "zijn",
        "als",
        "der",
        "die",
        "dat",
        "aan",
        "door",
        "over",
        "wordt",
        "worden",
        "niet",
        "onder",
        "tot",
        "bij",
        "naar",
        "artikel",
        "article",
        "lid",
        "eerste",
        "https",
        "http",
        "www",
        "com",
        "org",
        "html",
        "pdf",
    }
)
MIN_SECTION = 200
DEFAULT_BUDGET = 40_000


@dataclass(frozen=True)
class Passage:
    heading: str
    text: str
    score: float

    @property
    def size(self) -> int:
        return len(self.text)


def terms_of(text: str) -> set[str]:
    """The matchable terms of a question: names, codes and numbers."""
    found = {match.group(0).lower().strip(".-/") for match in TERM.finditer(text)}
    return {term for term in found if len(term) > 2 and term not in COMMON}


def strip_furniture(text: str) -> str:
    """Drop the navigation lines a page carries around its content."""
    kept = [
        line
        for line in text.splitlines()
        if not (len(line.strip()) < 80 and any(word in line.lower() for word in FURNITURE))
    ]
    return "\n".join(kept)


def outline(text: str) -> list[tuple[str, int]]:
    """Every heading the document declares, with where it starts."""
    return [(match.group(1).strip(), match.start()) for match in HEADING.finditer(text)]


def sections(text: str) -> list[tuple[str, str]]:
    """The document split at its own headings, preamble first."""
    marks = outline(text)
    if not marks:
        return [("", text)]
    found: list[tuple[str, str]] = []
    opening = text[: marks[0][1]].strip()
    if len(opening) >= MIN_SECTION:
        found.append(("", opening))
    for index, (heading, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if body:
            found.append((heading, body))
    return found


def score_section(heading: str, body: str, wanted: set[str]) -> float:
    """How many of the question's terms this section actually contains.

    Counted by distinct terms rather than occurrences, so a section that repeats
    one word does not outrank one that covers several. Deliberately not divided
    by length: in a law the long article is usually the obligation itself, and
    penalising it for being long is how the office came to read everything
    around Article 3 except Article 3.
    """
    if not wanted:
        return 0.0
    present = terms_of(f"{heading}\n{body}")
    return float(len(wanted & present))


def select_passages(
    text: str, question: str, budget: int = DEFAULT_BUDGET, keep_opening: bool = True
) -> list[Passage]:
    """The parts of a document worth reading for this question, within a budget.

    Returns them in document order, so what is read still reads as the document.
    A document with no shared terms is not discarded: its opening substantive
    sections are returned, which is still better than its navigation.
    """
    cleaned = strip_furniture(text)
    if len(cleaned) <= budget:
        return [Passage(heading="", text=cleaned, score=1.0)]
    wanted = terms_of(question)
    scored = [
        (index, heading, body, score_section(heading, body, wanted))
        for index, (heading, body) in enumerate(sections(cleaned))
    ]
    # A table-of-contents line names the article without containing it, and
    # scores well for being short. Substantive sections are ranked first.
    substantial = [row for row in scored if len(row[2]) >= MIN_SECTION]
    # Most of the question's terms first; among equals, the earlier section,
    # because a law states the obligation before it qualifies it.
    ranked = sorted(substantial or scored, key=lambda row: (-row[3], row[0]))
    chosen: dict[int, Passage] = {}
    spent = 0
    for index, heading, body, score in ranked:
        if score <= 0:
            continue
        if spent + len(body) > budget and chosen:
            continue
        chosen[index] = Passage(heading=heading, text=body[:budget], score=score)
        spent += min(len(body), budget)
        if spent >= budget:
            break
    if not chosen and keep_opening:
        # Nothing matched: read the document from where its content starts.
        for index, heading, body, score in scored:
            if spent + len(body) > budget and chosen:
                break
            chosen[index] = Passage(heading=heading, text=body[:budget], score=score)
            spent += min(len(body), budget)
    return [chosen[index] for index in sorted(chosen)]


def assemble(passages: list[Passage], skipped_note: bool = True) -> str:
    """The selected passages as one excerpt, saying that it is a selection."""
    parts: list[str] = []
    for passage in passages:
        parts.append(f"[{passage.heading}]\n{passage.text}" if passage.heading else passage.text)
    body = "\n\n".join(parts)
    if skipped_note and len(passages) > 1:
        body = (
            "Selected sections of a longer document, in its own order. "
            "Sections not shown were not selected for this question.\n\n" + body
        )
    return body
