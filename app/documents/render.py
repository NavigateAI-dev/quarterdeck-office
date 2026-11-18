"""Turn one artifact payload into a document.

Every renderer is a pure function of the stored payload, so an artifact written
months ago renders the same way today, and a payload from a type this office no
longer produces still reads as something.
"""

from __future__ import annotations

from typing import Any

from app.documents.edited import KEY as EDITED_KEY
from app.documents.edited import from_dict as edited_document
from app.documents.model import Block, Bullets, Document, Paragraph, Section, Sources, Table

LABEL = {
    "research_report": "Research",
    "research_summary": "Research summary",
    "strategy_proposal": "Strategy proposal",
    "task_plan": "Task plan",
    "critique": "Review",
    "final_report": "Final report",
    "research_subtask": "Research note",
}


def _source_line(value: Any) -> str:
    """A `Source` reads as its title and link, not as a dict."""
    if not isinstance(value, dict):
        return _text(value)
    title = _text(value.get("title"))
    url = _text(value.get("url"))
    if title and url:
        return f"{title} — {url}"
    return title or url or _text(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list | tuple):
        return ", ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_text(item)}" for key, item in sorted(value.items()))
    return str(value)


def _titleize(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _listing(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    return [text] if text else []


def _comparison(payload: dict[str, Any]) -> Table | None:
    """The researcher's comparison table, kept as a table.

    `ComparisonTable` holds rows as lists of cells and their sources alongside
    in `row_sources`, one per row. The source is put back on its own row, so it
    travels with the claim instead of being pooled at the end where nobody can
    tell which row it backs.
    """
    table = payload.get("comparison_table")
    if not isinstance(table, dict):
        return None
    columns = [_text(name) for name in table.get("columns") or []]
    rows = [
        [_text(cell) for cell in row]
        for row in table.get("rows") or []
        if isinstance(row, list | tuple)
    ]
    if not columns or not rows:
        return None
    sources = [_text(item) for item in table.get("row_sources") or []]
    if sources:
        columns = [*columns, "Source"]
        rows = [
            [*row, sources[index] if index < len(sources) else ""] for index, row in enumerate(rows)
        ]
    width = len(columns)
    squared = [[*row, *([""] * (width - len(row)))][:width] for row in rows]
    return Table(columns=columns, rows=squared)


# The answer itself: what the owner asked for. Shown first and alone, because
# a task's result used to open with its own engineering log and bury the
# recommendation in the middle of it.
ANSWER_FIELDS = ("recommendation", "verdict")
# The reader-first answer's own parts, placed by hand below.
READER_FIRST = ("lead", "next_steps", "not_checked")
# The reasoning that carries the answer, in the order it reads best.
REASONING = ("inferences", "implications", "comparison", "trade_offs")
# What the task was asked, kept next to the answer so it can be read against it.
ASKED = ("question", "research_question", "objective", "problem")
# Everything below is the working: true, useful, and not the answer.
LEADING = ASKED + ("confidence",)
# Fields rendered as their own section of bullets rather than a paragraph.
AS_LIST = (
    "key_findings",
    "findings",
    "facts",
    "implications",
    "comparison",
    "trade_offs",
    "limits",
    "open_questions",
    "acceptance_criteria",
    "expected_artifacts",
    "supporting_evidence",
    "unresolved",
    "changed_files",
    "unknowns",
    "assumptions",
    "inferences",
    "contradictory_evidence",
    "critical_issues",
    "weak_assumptions",
    "missing_evidence",
    "alternative_explanations",
    "required_corrections",
    "what_would_change_conclusion",
    "subquestions",
    "success_metrics",
)
# Where the evidence lives, whatever the artifact type calls it.
AS_SOURCES = ("sources", "citations", "official_sources")
# Bookkeeping a reader does not want in the body of a document.
# Carried by every synthesis because the V0 chain fed an engineering gate.
# On a research or marketing question they are scaffolding, and a task that
# proposed no engineering should not show an engineering scope at all.
ENGINEERING = ("proposed_engineering_scope", "engineering_acceptance_criteria")
HIDDEN = (
    "comparison_table",
    "scope_digest",
    "last_critique",
    "accessed_on",
    "task_kind",
    "answers",
    "blocked_sources",
    "coverage",
    # An owner's edit is shown as the document, or not at all.
    EDITED_KEY,
)


def _scorecard(scored: dict[str, Any]) -> list[Block]:
    """What a run delivered against what was asked, before anything else.

    A final report that opens with its recommendation reads as finished work
    even when the review refused it twice. This opens with the verdict.
    """
    asked = int(scored.get("questions_asked") or 0)
    answered = int(scored.get("questions_answered") or 0)
    unmet = _listing(scored.get("unmet_by_review"))
    verdict = _text(scored.get("last_review_verdict"))
    settled = verdict.upper() == "PASS" and not unmet and (asked == 0 or answered == asked)
    lead = (
        "This met what was asked."
        if settled
        else "This did not fully answer what was asked. What follows is partial."
    )
    blocks: list[Block] = [Paragraph(lead)]
    if asked:
        blocks.append(Paragraph(f"Questions answered: {answered} of {asked}."))
    unanswered = _listing(scored.get("unanswered_questions"))
    if unanswered:
        blocks.append(Paragraph("Not answered:"))
        blocks.append(Bullets(items=unanswered))
    if verdict and verdict.lower() != "none":
        blocks.append(Paragraph(f"Last review verdict: {verdict}."))
    if unmet:
        blocks.append(Paragraph("The review raised, and these were not resolved:"))
        blocks.append(Bullets(items=unmet))
    blocked = _listing(scored.get("blocked_sources"))
    if blocked:
        blocks.append(Paragraph("Sources that refused us:"))
        blocks.append(Bullets(items=blocked))
    reason = _text(scored.get("stopped_reason"))
    if reason:
        blocks.append(Paragraph(f"Stopped because: {reason}."))
    criteria = _listing(scored.get("acceptance_criteria"))
    if criteria:
        blocks.append(Paragraph("It was asked to deliver:"))
        blocks.append(Bullets(items=criteria))
    return blocks


def _did_engineering(payload: dict[str, Any]) -> bool:
    """Whether this task actually produced engineering work."""
    return bool(
        payload.get("branch") or payload.get("changed_files") or payload.get("merge_request")
    )


def _body(payload: dict[str, Any]) -> list[Section]:
    """The deliverable an agent wrote, as one section per headed block."""
    written = payload.get("deliverable_body") or payload.get("slides")
    if not isinstance(written, list):
        return []
    sections: list[Section] = []
    for slide in written:
        if not isinstance(slide, dict):
            continue
        heading = _text(slide.get("heading"))
        points = _listing(slide.get("points"))
        if heading and points:
            blocks: list[Block] = [Bullets(items=points)]
            table = _body_table(slide.get("table"))
            if table is not None:
                blocks.append(table)
            sections.append(Section(heading=heading, blocks=blocks))
    return sections


def _body_table(given: Any) -> Table | None:
    """A block's numbers as a table, squared to its columns."""
    if not isinstance(given, dict):
        return None
    columns = [_text(name) for name in given.get("columns") or []][:6]
    rows = [
        [_text(cell) for cell in row]
        for row in (given.get("rows") or [])[:24]
        if isinstance(row, list | tuple)
    ]
    if len(columns) < 2 or not rows:
        return None
    width = len(columns)
    return Table(columns=columns, rows=[[*row, *([""] * width)][:width] for row in rows])


def document_for(artifact_type: str, payload: dict[str, Any], title: str | None = None) -> Document:
    """A readable document for one stored artifact payload.

    The answer first, then the working. A reader who wants only the answer
    should find it in the first thing they read.
    """
    edited = payload.get(EDITED_KEY)
    if edited is not None:
        # The owner's own edit is the answer; a broken one is not trusted.
        try:
            return edited_document(edited)
        except ValueError:
            pass
    kind = LABEL.get(artifact_type, _titleize(artifact_type))
    heading = title or _text(payload.get("question") or payload.get("objective")) or kind
    # Written for the reader: the lead is the answer, and the reasoning
    # behind it is working the reader can open, not the first thing they read.
    leads = bool(_text(payload.get("lead")))
    answer = _answer_blocks(payload, leads)
    sections: list[Section] = []
    if answer:
        sections.append(Section(heading="The answer", blocks=answer))
    opening = _asked_blocks(payload, heading)
    if opening:
        sections.append(Section(heading="What was asked", blocks=opening, supporting=bool(answer)))
    scored = payload.get("coverage")
    if isinstance(scored, dict):
        sections.insert(0, Section(heading="Did this answer the ask?", blocks=_scorecard(scored)))
    sections = _with_questions(sections, payload)
    sections += _blocked_sections(payload)
    table = _comparison(payload)
    if table is not None:
        # A comparison is usually the answer to "which one", so it stays up.
        sections.append(Section(heading="Comparison", blocks=[table]))
    seen = {*LEADING, *AS_SOURCES, *HIDDEN, *READER_FIRST}
    sections += _working_sections(payload, leads, bool(answer), seen)
    sections += _remainder_and_sources(payload, seen, bool(answer))
    sections = _in_reading_order(payload, sections, leads)
    # The label repeats the title when a task has no objective of its own.
    return Document(title=heading, subtitle=None if kind == heading else kind, sections=sections)


def _answer_blocks(payload: dict[str, Any], leads: bool) -> list[Block]:
    """The lead, or the answer fields and the reasoning when there is no lead."""
    answer: list[Block] = []
    if leads:
        answer.append(Paragraph(_text(payload["lead"])))
    for key in () if leads else ANSWER_FIELDS:
        if _text(payload.get(key)):
            answer.append(Paragraph(_text(payload[key])))
    for key in () if leads else REASONING:
        items = _listing(payload.get(key))
        if items:
            answer.append(Paragraph(_titleize(key) + ":"))
            answer.append(Bullets(items=items))
    return answer


def _asked_blocks(payload: dict[str, Any], heading: str) -> list[Block]:
    """What was asked, without repeating the title."""
    opening: list[Block] = []
    for key in LEADING:
        value = payload.get(key)
        if key in ASKED and heading == _text(value):
            continue
        if _text(value):
            opening.append(Paragraph(f"{_titleize(key)}: {_text(value)}"))
    return opening


def _with_questions(sections: list[Section], payload: dict[str, Any]) -> list[Section]:
    """Coverage first, then each question asked and how it was answered."""
    answers = [a for a in payload.get("answers") or [] if isinstance(a, dict)]
    if not answers:
        return sections
    # Coverage first: a report that answered one of four questions says so
    # before anything it did find.
    counted: dict[str, int] = {}
    for entry in answers:
        status = _text(entry.get("status")) or "unanswered"
        counted[status] = counted.get(status, 0) + 1
    line = f"Answered {counted.get('answered', 0)} of {len(answers)} questions asked"
    rest = [f"{count} {name}" for name, count in sorted(counted.items()) if name != "answered"]
    if rest:
        line += f" ({', '.join(rest)})"
    sections = [Section(heading="Coverage", blocks=[Paragraph(line + ".")]), *sections]
    for entry in answers:
        blocks: list[Block] = [
            Paragraph(f"Status: {_text(entry.get('status')) or 'unanswered'}"),
            Paragraph(_text(entry.get("answer"))),
        ]
        missing = _listing(entry.get("missing"))
        if missing:
            blocks.append(Paragraph("Still needed:"))
            blocks.append(Bullets(items=missing))
        backing = _listing(entry.get("evidence"))
        if backing:
            blocks.append(Sources(items=backing))
        heading_text = _text(entry.get("question")) or "Question"
        sections.append(Section(heading=heading_text, blocks=blocks, supporting=True))
    return sections


def _blocked_sections(payload: dict[str, Any]) -> list[Section]:
    blocked = [b for b in payload.get("blocked_sources") or [] if isinstance(b, dict)]
    if not blocked:
        return []
    items = [f"{_text(item.get('url'))} — {_text(item.get('status'))}" for item in blocked]
    return [
        Section(
            supporting=True,
            heading="Sources that refused us",
            blocks=[Bullets(items=items)],
        )
    ]


def _working_sections(
    payload: dict[str, Any], leads: bool, has_answer: bool, seen: set[str]
) -> list[Section]:
    """The recommendation under a lead, and each listed field not already shown.
    Marks what it shows in `seen`."""
    sections: list[Section] = []
    if not leads:
        seen.update({*ANSWER_FIELDS, *REASONING})
    elif _text(payload.get("recommendation")):
        sections.append(
            Section(
                heading="Recommendation",
                blocks=[Paragraph(_text(payload["recommendation"]))],
                supporting=True,
            )
        )
        seen.add("recommendation")
    if not _did_engineering(payload):
        # Nothing was built, so an engineering scope is scaffolding.
        seen.update(ENGINEERING)
    for key in AS_LIST:
        if key in seen:
            # Already carried by the answer; showing it twice makes the working
            # look like new information.
            continue
        items = _listing(payload.get(key))
        seen.add(key)
        if items:
            sections.append(
                Section(
                    heading=_titleize(key), blocks=[Bullets(items=items)], supporting=has_answer
                )
            )
    return sections


def _remainder_and_sources(
    payload: dict[str, Any], seen: set[str], has_answer: bool
) -> list[Section]:
    """Anything no rule above claimed, then the sources."""
    sections: list[Section] = []
    remainder: list[Block] = []
    for key, value in sorted(payload.items()):
        if key in ("deliverable_body", "slides") or key in seen or not _text(value):
            continue
        items = _listing(value)
        one_line = f"{_titleize(key)}: {_text(value)}"
        remainder.append(Bullets(items=items) if len(items) > 1 else Paragraph(one_line))
    if remainder:
        sections.append(Section(heading="Also recorded", blocks=remainder, supporting=has_answer))
    evidence: list[str] = []
    for key in AS_SOURCES:
        value = payload.get(key)
        if isinstance(value, list | tuple):
            evidence.extend(line for line in (_source_line(item) for item in value) if line)
        else:
            evidence.extend(_listing(value))
    if evidence:
        sections.append(
            Section(heading="Sources", blocks=[Sources(items=evidence)], supporting=has_answer)
        )
    return sections


def _in_reading_order(
    payload: dict[str, Any], sections: list[Section], leads: bool
) -> list[Section]:
    """The deliverable the owner asked for leads, in the order it will be read
    or spoken; the report's own sections become the working beneath it.
    Without this a "presentation" is the answer, the facts and the unknowns:
    a report with page breaks."""
    written = _body(payload)
    if leads:
        # The answer, the useful part, what to do next, what is open; then the
        # working, folded beneath.
        first = [row for row in sections if row.heading == "The answer"]
        after: list[Section] = []
        steps = _listing(payload.get("next_steps"))
        if steps:
            after.append(Section(heading="What to do next", blocks=[Bullets(items=steps)]))
        if _text(payload.get("not_checked")):
            after.append(
                Section(
                    heading="What we could not check",
                    blocks=[Paragraph(_text(payload["not_checked"]))],
                )
            )
        return (
            first
            + written
            + after
            + [
                Section(heading=row.heading, blocks=row.blocks, supporting=True)
                for row in sections
                if row.heading != "The answer"
            ]
        )
    if written:
        return written + [
            Section(heading=row.heading, blocks=row.blocks, supporting=True) for row in sections
        ]
    return sections
