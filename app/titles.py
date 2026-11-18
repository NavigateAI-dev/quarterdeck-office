"""A short title for a task, taken from what was asked.

A task's objective is the ask: the outcome, the context, the constraints, often
several paragraphs of it. The office was setting all of that in bold at the top
of the room, which left the title unable to do the one thing a title is for —
telling one task from another at a glance.

Derived in code and not by a model, deliberately. A title is read before
anything has happened, on every task, in lists and breadcrumbs and notification
lines; it should cost nothing, never vary between two readings of the same
task, and never surprise the person who typed the objective. What is dropped is
never lost: the objective is shown directly beneath it.
"""

from __future__ import annotations

import re

TITLE_LIMIT = 72
# Words that open a request rather than describe the work itself. A title
# saying "Please can you research…" wastes its first third.
OPENERS = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|i(?:'| a)?m?\s+want(?:ing)?\s+to\s+|"
    r"i need (?:you )?to\s+|we need to\s+|let's\s+|lets\s+|help me\s+|"
    r"the (?:goal|objective|aim|task) is to\s+)+",
    re.IGNORECASE,
)
# Where a sentence stops being the ask and starts being the context.
BREAKS = (
    "\n",
    " so that ",
    " so we ",
    " because ",
    " in order to ",
    " — ",
    "; ",
    ". ",
    "。",
    "，",
)
# A question mark is part of the ask, not punctuation to tidy away:
# "And what would it cost?" reads wrong without it.
TRIM = " .,:;-—。，"


def title_for(objective: str) -> str:
    """One line naming this task, at most TITLE_LIMIT characters."""
    text = " ".join((objective or "").strip().split("\n")[0].split())
    if not text:
        return "Untitled task"
    text = OPENERS.sub("", text).strip(TRIM)
    for separator in BREAKS:
        head = text.split(separator)[0].strip()
        # Only if something survives: a question that opens with a break would
        # otherwise title itself with nothing.
        if head:
            text = head
    if len(text) > TITLE_LIMIT:
        # Cut at a word so a title does not end mid-word, unless there is no
        # word boundary to cut at — which is true of Chinese and Japanese.
        cut = text[:TITLE_LIMIT].rsplit(" ", 1)[0]
        text = (cut if len(cut) > TITLE_LIMIT // 3 else text[:TITLE_LIMIT]) + "…"
    text = text.strip(TRIM)
    if not text:
        return "Untitled task"
    return text[0].upper() + text[1:] if text[0].isascii() else text


def title_of(title: str | None, objective: str) -> str:
    """The title the owner gave the task, or one derived from what they asked."""
    given = " ".join((title or "").split())
    return given or title_for(objective)
