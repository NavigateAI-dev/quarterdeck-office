"""Conversational replies in chat, grounded in office facts.

The model reads a snapshot of the office (status, projects, recent runs and
their results) and answers in plain language. It may look things up first and
offer work as a task (`app.channels.lookup`); it never approves or
changes anything itself. Chat has its own daily token allowance per
bot so conversation never spends a run's budget.

An agent's bot answers as that agent: its instructions and instruction-only
skills are added, and its facts cover only the runs it took part in.
"""

import json
from datetime import UTC, datetime
from typing import Any

RULES = """Answer from the office facts below, the conversation so far and, when
you looked something up, what you found. Be brief and concrete: a few sentences
or a short list, Slack formatting, no headings. Refer to runs by their short id
and project. Never invent runs, results or numbers.
What the office can do from right here (QA2): look things up now (search the
web, read a page, open a page in its own browser and see it, as for a weather
radar), read pictures and text files the owner shares, and take on bigger work
as a task (research, analysis, documents, dashboards, building software) that
runs in the office and reports back in this thread once the owner presses
Start. Never send the owner to their computer to ask for something you can
offer here. Approvals are the one thing made only in the office itself.
Today is {today}."""

OFFICE = "You are the owner's AI office, answering them in Slack.\n"

# Where each role's own output lives in a run's state.
ROLE_OUTPUT = {
    "chief": "plan",
    "researcher": "research",
    "strategist": "strategy",
    "critic": "critique",
}


def agent_persona(entry: dict[str, Any], place: str = "in Slack") -> str:
    """The agent's voice: who it is, its owner's instructions, its skills.

    Skills that declare permissions need tools a chat reply does not have, so
    they are left out here exactly as a run would skip them.
    """
    lines = [
        f"You are {entry['name']}, the {entry['role']} agent of the owner's AI office, "
        f"answering the owner {place} as yourself."
    ]
    if entry.get("instructions"):
        lines.append(f"Your owner's instructions:\n{entry['instructions']}")
    for skill in entry.get("skills") or []:
        if not skill.get("permissions"):
            lines.append(f"Skill '{skill['name']}':\n{skill['instructions']}")
    return "\n\n".join(lines) + "\n"


def build_messages(
    facts: dict[str, Any],
    history: list[dict[str, str]],
    text: str,
    persona: str | None = None,
) -> list[dict[str, Any]]:
    system = (persona or OFFICE) + RULES.format(today=datetime.now(UTC).date().isoformat())
    system += "\n\nOffice facts (JSON):\n" + json.dumps(facts, default=str)[:24_000]
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Roughly, before the call: text by its length, a picture as a fixed amount."""
    total = 0
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            total += len(content) // 3
            continue
        for part in content:
            if part.get("type") == "image_url":
                total += 1500
            else:
                total += len(str(part.get("text") or "")) // 3
    return total


def used_tokens(prompt: int | None, completion: int | None, reserved: int) -> int:
    """Unknown usage is charged at the reservation, never as free."""
    if prompt is None or completion is None:
        return reserved
    return prompt + completion
