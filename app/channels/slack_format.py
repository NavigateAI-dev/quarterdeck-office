"""What the office posts in Slack: the answer as Slack text, and its buttons.

Slack writes bold as `*x*`, italic as `_x_` and links as `<address|label>`; a
document's text writes them as Markdown does, so each mark is translated, and
`&`, `<` and `>` are escaped so text can never become a Slack mention or link.

A message is at most 50 blocks of at most 3000 characters each. The answer is
cut well before that: the whole document goes with it as a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.documents.inline import segments
from app.documents.model import Bullets, Document, Paragraph, Section, Sources, Table

BLOCK_TEXT = 2900
ANSWER_LIMIT = 6000
TABLE_ROWS = 8

# What each button does, as Slack sends it back.
START = "office_start"
NEW = "office_new"
NOT_NOW = "office_not_now"
STOP = "office_stop"
REWORK = "office_rework"
SKIP = "office_skip"
RETRY = "office_retry"
OPEN = "office_open"
# An approval decided from Slack: only a low-risk one carries these buttons.
APPROVE = "office_approve"
REJECT = "office_reject"
DECISIONS = frozenset({APPROVE, REJECT})
# A research brief kept in Knowledge.
SAVE = "office_save"
ACTIONS = frozenset({START, NEW, NOT_NOW, STOP, REWORK, SKIP, RETRY, APPROVE, REJECT, SAVE})
# Clicking one of these is the same as typing the word in the thread.
TYPED = {START: "yes", NEW: "new", NOT_NOW: "not now"}


# A card's tone, shown as a dot before its status: Slack has no coloured edge
# for Block Kit messages outside its older attachments.
TONES = {
    "running": "🔵",
    "done": "✅",
    "waiting": "🟡",
    "failed": "🔴",
    "alert": "🔴",
    "info": "⚪",
}
FIELD_TEXT = 1900
MAX_FIELDS = 10


@dataclass(frozen=True)
class Card:
    """One message the office posts, laid out the same way whatever it is about.

    A context line (tone, status, kind, who and where), the title, the body,
    two columns of facts, a footer, and the buttons last. The body and the
    footer are Slack text already; everything else is escaped here.
    """

    kind: str
    title: str
    tone: str
    status: str
    about: tuple[str, ...] = ()
    body: str = ""
    facts: tuple[tuple[str, str], ...] = ()
    footer: str = ""
    buttons: tuple[dict[str, Any], ...] = ()


def escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mrkdwn(text: str) -> str:
    """A line of document text in Slack's own marks."""
    out: list[str] = []
    for run in segments(text):
        shown = escaped(run.text)
        if run.link:
            out.append(f"<{run.link}|{shown.replace('|', '¦')}>")
        elif run.bold:
            out.append(f"*{shown}*")
        elif run.italic:
            out.append(f"_{shown}_")
        else:
            out.append(shown)
    return "".join(out)


def _table(table: Table) -> list[str]:
    lines = []
    for row in table.rows[:TABLE_ROWS]:
        pairs = [
            f"{escaped(column)}: {mrkdwn(cell)}"
            for column, cell in zip(table.columns, row, strict=False)
            if cell.strip()
        ]
        lines.append("• " + "; ".join(pairs))
    if len(table.rows) > TABLE_ROWS:
        lines.append(f"_…and {len(table.rows) - TABLE_ROWS} more rows in the file._")
    return lines


def _section(section: Section) -> list[str]:
    lines = [f"*{escaped(section.heading)}*"] if section.heading else []
    for block in section.blocks:
        if isinstance(block, Paragraph):
            lines.append(mrkdwn(block.text))
        elif isinstance(block, Bullets):
            lines += [
                f"{index}. {mrkdwn(item)}" if block.ordered else f"• {mrkdwn(item)}"
                for index, item in enumerate(block.items, start=1)
            ]
        elif isinstance(block, Table):
            lines += _table(block)
        elif isinstance(block, Sources):
            continue  # Kept for the file, where a reader can follow them.
    return lines


def answer_text(document: Document, limit: int = ANSWER_LIMIT) -> tuple[str, bool]:
    """The answer's own sections as Slack text, and whether it was cut."""
    paragraphs: list[str] = []
    for section in document.answer():
        paragraphs.append("\n".join(_section(section)))
    text = "\n\n".join(part for part in paragraphs if part.strip())
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    # At a paragraph or line end, so a mark or link is not broken in two.
    end = max(cut.rfind("\n\n"), cut.rfind("\n"))
    return (cut[:end] if end > limit // 2 else cut).rstrip(), True


def chunks(text: str, size: int = BLOCK_TEXT) -> list[str]:
    """Text in pieces Slack takes as one block each, split at line ends."""
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:size])
            line = line[size:]
        joined = f"{current}\n{line}" if current else line
        if len(joined) > size:
            pieces.append(current)
            current = line
        else:
            current = joined
    if current.strip():
        pieces.append(current)
    return [piece for piece in pieces if piece.strip()]


def sections(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": piece}} for piece in chunks(text)
    ]


def button(
    label: str,
    action: str,
    value: str = "",
    style: str | None = None,
    url: str | None = None,
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    found: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": label[:75]},
        "action_id": action,
    }
    if value:
        found["value"] = value[:2000]
    if style:
        found["style"] = style
    if url:
        found["url"] = url
    if confirm:
        # Slack asks "are you sure?" itself, before anything reaches the office.
        title, body = confirm
        found["confirm"] = {
            "title": {"type": "plain_text", "text": title[:100]},
            "text": {"type": "plain_text", "text": body[:300]},
            "confirm": {"type": "plain_text", "text": label[:30]},
            "deny": {"type": "plain_text", "text": "Cancel"},
        }
    return found


def actions(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {"type": "actions", "elements": list(buttons)}


def proposal_blocks(text: str, likely_project: bool) -> list[dict[str, Any]]:
    """A proposal: Start, or a new project when one is only likely, or not now."""
    buttons = [button("Start", START, style="primary")]
    if likely_project:
        buttons.append(button("New project instead", NEW))
    buttons.append(button("Not now", NOT_NOW))
    return [*sections(text), actions(*buttons)]


def open_button(link: str | None) -> list[dict[str, Any]]:
    return [button("Open in the office", OPEN, url=link)] if link else []


def finished_blocks(text: str, run_id: str, link: str | None) -> list[dict[str, Any]]:
    return [*sections(text), actions(*rework_buttons(run_id, link))]


def _context_line(card: Card) -> str:
    dot = TONES.get(card.tone, TONES["info"])
    parts = [f"{dot} *{escaped(card.status)}*", escaped(card.kind)]
    parts += [escaped(part) for part in card.about if part]
    return "  ·  ".join(parts)


def card_text(card: Card) -> str:
    """The card as one text: what a notification, a search and a screen reader get."""
    lines = [_context_line(card), f"*{escaped(card.title)}*"]
    if card.body:
        lines.append(card.body)
    if card.facts:
        lines.append(
            "  ·  ".join(f"{escaped(label)}: {escaped(value)}" for label, value in card.facts)
        )
    if card.footer:
        lines.append(card.footer)
    return "\n".join(lines)


def card_blocks(card: Card) -> list[dict[str, Any]]:
    """The card in Block Kit. The buttons, when there are any, are the last block."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _context_line(card)[:BLOCK_TEXT]}],
        },
        *sections(f"*{escaped(card.title)}*"),
    ]
    if card.body:
        blocks += sections(card.body)
    if card.facts:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*{escaped(label)}*\n{escaped(value)}"[:FIELD_TEXT],
                    }
                    for label, value in card.facts[:MAX_FIELDS]
                ],
            }
        )
    if card.footer:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": card.footer[:BLOCK_TEXT]}]}
        )
    if card.buttons:
        blocks.append(actions(*card.buttons))
    return blocks


def task_buttons(
    run_id: str, link: str | None, running: bool, stuck: bool
) -> tuple[dict[str, Any], ...]:
    """What the owner can do with a working task at this moment."""
    buttons: list[dict[str, Any]] = []
    if stuck:
        buttons += [button("Skip this step", SKIP, run_id), button("Try again", RETRY, run_id)]
    if running or stuck:
        buttons.append(
            button(
                "Stop",
                STOP,
                run_id,
                style="danger",
                confirm=("Stop this task?", "The work done so far is kept."),
            )
        )
    return (*buttons, *open_button(link))


def rework_buttons(run_id: str, link: str | None) -> tuple[dict[str, Any], ...]:
    return (
        button(
            "Rework",
            REWORK,
            run_id,
            confirm=(
                "Rework this task?",
                "The office reworks the answer where it is weakest. It may use paid "
                "model calls. To say what to change, reply in this thread instead.",
            ),
        ),
        *open_button(link),
    )


def approval_buttons(
    approval_id: str, digest: str, link: str | None, low_risk: bool
) -> tuple[dict[str, Any], ...]:
    """Approve and Reject for a low-risk proposal; otherwise only the way to read it.

    The buttons carry the proposal's digest, so a press made against a
    proposal that has changed since is refused.
    """
    seen = (button("See exactly what", OPEN, url=link),) if link else ()
    if not low_risk:
        return seen
    value = f"{approval_id}:{digest}"
    return (
        button(
            "Approve",
            APPROVE,
            value,
            style="primary",
            confirm=("Approve this?", "It records the decision and the task goes on."),
        ),
        button(
            "Reject",
            REJECT,
            value,
            confirm=("Reject this?", "The task stops here. Its work so far is kept."),
        ),
        *seen,
    )
