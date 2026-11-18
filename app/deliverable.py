"""What the owner wants at the end, said before the work starts.

The office decided the shape of its own output: a chain that always produced a
prose synthesis, whatever was asked. A request to compare vendors wants a table;
a request to brief a client wants a page; a request for a board update wants
slides. Saying so up front changes what the work aims at, not only how it is
filed afterwards.

**One place.** A shape is declared here and nowhere else: its label, the
instruction its agents are told, the format it suits, and whether the body it
writes leads the finished document. Adding slides once cost eight edits — this
table, the agent's output schema, the Chief's instructions, the renderer, the
file writer, the task dialog, a migration and a rebuild. It now costs one entry,
because every shape that needs structure writes the same thing: a list of headed
blocks, in `deliverable_body`. A deck is headed blocks. A brief is headed
blocks. So is a checklist, an agenda or a decision memo.

A shape the office cannot honour is not offered, and a format it cannot write
becomes no file rather than a file nobody asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Shape:
    """One way a task can come out.

    `leads` says whether the body the agent writes stands in front of the
    report's own sections. A deck is its slides; a report is its reasoning, and
    a body would only repeat it.
    """

    id: str
    label: str
    instruction: str
    # The format it is handed over as unless the owner says otherwise.
    suits: str
    leads: bool = False
    # What the agent is told about the body it writes, when the body leads.
    body: str = ""


# How it is handed over. "onscreen" means the office keeps it and writes no file.
FORMATS: dict[str, str] = {
    "onscreen": "On screen only",
    "docx": "Word",
    "xlsx": "Excel",
    "pptx": "PowerPoint",
    "html": "Web page",
    "md": "Markdown",
}

REGISTERED: dict[str, Shape] = {}


def register(shape: Shape) -> Shape:
    """Declare a way a task can come out. The only step a new shape needs."""
    if shape.suits not in FORMATS:
        raise ValueError(f"{shape.id} suits {shape.suits}, which the office cannot write")
    if shape.leads and not shape.body:
        raise ValueError(f"{shape.id} leads with a body but says nothing about writing it")
    REGISTERED[shape.id] = shape
    return shape


register(
    Shape(
        id="report",
        label="A written report",
        instruction=(
            "The owner wants a written report: the answer stated plainly, then the reasoning "
            "and the evidence behind it."
        ),
        suits="docx",
    )
)
register(
    Shape(
        id="comparison",
        label="A comparison",
        instruction=(
            "The owner wants a comparison. The table is the answer: name the options as rows "
            "and the dimensions as columns, cite a source per row, and write 'not documented' "
            "where you could not verify a cell rather than leaving it out."
        ),
        suits="xlsx",
    )
)
register(
    Shape(
        id="brief",
        label="A one-page brief",
        instruction=(
            "The owner wants one page: the answer, what it rests on, and what would change it. "
            "Leave out anything that does not serve a decision."
        ),
        suits="docx",
        leads=True,
        body=(
            "one block per point the page makes, in reading order, each a short heading and "
            "the few lines under it. Nothing that does not serve a decision."
        ),
    )
)
register(
    Shape(
        id="dashboard",
        label="A dashboard with charts",
        instruction=(
            "The owner wants a dashboard: the numbers that matter, each shown so it can be "
            "read at a glance, with a line on what it means. Find real figures and cite them."
        ),
        suits="html",
        leads=True,
        body=(
            "one block per figure or finding: a short heading, up to six points saying what it "
            "shows and why it matters, and, wherever there are numbers, a `table` of them: the "
            "first column the label or the period (2024, Q3 2025, March), the other columns "
            "plain numbers with the unit in the column heading (such as 'Revenue (EUR k)'), no "
            "units or words in the number cells. The office draws the charts from these tables."
        ),
    )
)
register(
    Shape(
        id="software",
        label="Working software (a website or app)",
        instruction=(
            "The owner wants working software, such as a website or a small app, built and "
            "checked in the office's project folder. The answer says what was built, how to "
            "open and use it, and what is left to do."
        ),
        # The software is the deliverable: its files, preview and zip, not a document.
        suits="onscreen",
    )
)
register(
    Shape(
        id="slides",
        label="Slides",
        instruction=(
            "The owner wants slides. Write for a spoken audience: one idea per slide, a short "
            "heading and a few lines under it, no line longer than a person will read aloud."
        ),
        suits="pptx",
        leads=True,
        body=(
            "one block per slide, in the order they will be spoken, each a short heading and "
            "the few lines said under it. Follow any structure the owner asked for, slide for "
            "slide."
        ),
    )
)

DEFAULT: dict[str, str] = {"shape": "report", "format": "onscreen"}


def clean(given: dict[str, Any] | None) -> dict[str, str]:
    """A deliverable the office can actually honour, or the default."""
    if not given:
        return dict(DEFAULT)
    shape = str(given.get("shape") or DEFAULT["shape"])
    if shape not in REGISTERED:
        shape = DEFAULT["shape"]
    wanted = str(given.get("format") or "")
    if wanted not in FORMATS:
        # A format the office cannot write becomes no file at all, never a
        # file the owner did not ask for.
        wanted = DEFAULT["format"]
    return {"shape": shape, "format": wanted}


def shape_of(deliverable: dict[str, Any] | None) -> Shape:
    """The registered shape this deliverable asks for."""
    return REGISTERED[clean(deliverable)["shape"]]


def leads(deliverable: dict[str, Any] | None) -> bool:
    """Whether the body the agent writes stands in front of the document."""
    return shape_of(deliverable).leads


# Every agent is told what the work should come to, but only the last one
# writes it. Without this the critic refused a deck for not existing yet —
# which it could not, at the step the critic reads — and the revision loop
# could never converge on any shape whose body leads.
WRITTEN_LAST = (
    "The deliverable itself is written at the final step. Earlier steps are judged on their "
    "evidence and reasoning, never on whether the deliverable exists yet."
)


def asked_for(deliverable: dict[str, Any] | None) -> str:
    """One line telling the agents what the work should come to."""
    chosen = clean(deliverable)
    shape = REGISTERED[chosen["shape"]]
    line = shape.instruction
    if shape.leads:
        line += f" Write it into `deliverable_body`: {shape.body} {WRITTEN_LAST}"
    if chosen["format"] != "onscreen":
        line += f" It will be handed over as {FORMATS[chosen['format']]}."
    return line


def describe(deliverable: dict[str, Any] | None) -> str:
    """What the owner asked for, in their own terms."""
    chosen = clean(deliverable)
    label = REGISTERED[chosen["shape"]].label
    if chosen["format"] == "onscreen":
        return f"{label}, on screen"
    return f"{label}, as {FORMATS[chosen['format']]}"


def offered() -> list[dict[str, Any]]:
    """Every shape the office can produce, for the owner to choose from.

    Read from the registry rather than copied out of it, so a shape registered
    anywhere is offered everywhere without a second list to keep in step.
    """
    return [
        {"id": shape.id, "label": shape.label, "suits": shape.suits}
        for shape in REGISTERED.values()
    ]
