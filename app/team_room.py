"""The task's room read as a conversation (T): grouped by the stage each post
was said in, with replies under the post they answer."""

import re
from typing import Any

WORK_KINDS = ("claim", "finding", "doubt", "draft")
HUDDLE_ROLES = ("critic", "writer", "advisor")
# What the Chief says when it acts on a post, and what it quotes of it.
ACTED_ON = re.compile(
    r"^(?:Added \S+ at the \w+'s offer|Rerunning (?P<rerun>\S+) to answer"
    r"|Kept as a limit for (?P<limit>\S+)): (?P<quoted>.+)$"
)


def _quoted(text: str) -> str:
    return text.rstrip("…").strip()


def _staged(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each post with its stage. A post that kept its own stage keeps it; one
    written before posts did is read into one from the order of the room."""
    staged: list[dict[str, Any]] = []
    wave = 0
    phase: str | None = None
    for raw in posts:
        item = dict(raw)
        if item.get("phase"):
            phase, wave = item["phase"], int(item.get("wave") or wave)
        elif item.get("kind") != "owner":
            kind, author, text = item.get("kind"), item.get("author_role"), item.get("text") or ""
            if kind in WORK_KINDS and item.get("step_id"):
                if phase != "work":
                    wave += 1
                phase = "work"
            elif author == "chief" and ACTED_ON.match(text):
                phase = "huddle"
            elif kind == "plan":
                phase = "plan"
            elif author == "chief" and kind == "note":
                phase = "answer"
            elif kind == "advice":
                phase = "answer" if phase in ("review", "answer") else "plan"
            elif author == "critic" and (
                item.get("to_role") or phase == "review" or text.startswith("The work passes")
            ):
                phase = "review"
            elif author in HUDDLE_ROLES and wave:
                phase = "huddle"
            item["phase"], item["wave"] = phase, wave or None
        staged.append(item)
    return staged


def _parent(item: dict[str, Any], before: list[dict[str, Any]]) -> str | None:
    """The post this one answers, among those said before it."""
    keys = {p["key"] for p in before}
    if item.get("reply_to"):
        return item["reply_to"] if item["reply_to"] in keys else None
    acted = ACTED_ON.match(item.get("text") or "") if item.get("author_role") == "chief" else None
    if acted:
        quoted = _quoted(acted["quoted"])
        step = acted["rerun"] or acted["limit"]
        for earlier in reversed(before):
            if step and earlier.get("to_step") != step:
                continue
            if not step and earlier.get("kind") != "offer":
                continue
            if str(earlier.get("text") or "").startswith(quoted):
                return str(earlier["key"])
        return None
    target = item.get("to_step")
    if target and item.get("phase") == "huddle":
        for earlier in reversed(before):
            if earlier.get("step_id") == target:
                return str(earlier["key"])
    return None


def _label(phase: str | None, wave: int | None, counts: dict[str, int]) -> str:
    counts[phase or ""] = counts.get(phase or "", 0) + 1
    if phase == "plan":
        return f"Replanning after wave {wave}" if wave else "Planning"
    if phase == "work":
        return f"Wave {wave}"
    if phase == "huddle":
        return f"Huddle {counts['huddle']}" + (f" · after wave {wave}" if wave else "")
    if phase == "review":
        return "Review" if counts["review"] == 1 else f"Review {counts['review']}"
    if phase == "answer":
        return "Answer"
    return "Room"


def threaded(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The room as stages, oldest first; each stage's posts carry their replies."""
    # A post without a key (a hand-built one) is still placed, by its position.
    staged = [p if p.get("key") else {**p, "key": f"#{n}"} for n, p in enumerate(_staged(posts), 1)]
    by_key: dict[str, dict[str, Any]] = {}
    sections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for index, item in enumerate(staged):
        node = {**item, "replies": []}
        parent = _parent(item, staged[:index])
        by_key[item["key"]] = node
        if parent is not None and parent in by_key:
            by_key[parent]["replies"].append(node)
            by_key[parent]["_section"]["count"] += 1
            node["_section"] = by_key[parent]["_section"]
            continue
        stage = (item.get("phase"), item.get("wave"))
        joins = item.get("kind") == "owner" and not item.get("phase") and sections
        if not sections or (not joins and sections[-1]["_stage"] != stage):
            sections.append(
                {
                    "_stage": stage,
                    "phase": stage[0],
                    "wave": stage[1],
                    "label": _label(stage[0], stage[1], counts),
                    "posts": [],
                    "count": 0,
                }
            )
        section = sections[-1]
        section["posts"].append(node)
        section["count"] += 1
        node["_section"] = section
    for node in by_key.values():
        node.pop("_section", None)
    for section in sections:
        section.pop("_stage", None)
    return sections


# How the project room says what each role did in a huddle, in order.
SAID = (
    ("challenge", "challenge", "challenges"),
    ("need", "request", "requests"),
    ("offer", "offer", "offers"),
    ("note", "note", "notes"),
)
ROLE_TITLE = {"critic": "Critic", "writer": "Writer", "advisor": "Advisor", "owner": "You"}
CHIEF_DID = (
    (re.compile(r"^Added (\S+) at"), "added {}"),
    (re.compile(r"^Rerunning (\S+) to answer"), "reran {}"),
    (re.compile(r"^Kept as a limit for (\S+):"), "kept a limit for {}"),
)
QUOTE = 140


def huddle_summary(posts: list[dict[str, Any]], wave: int) -> str:
    """One huddle in one line, from its posts alone: who raised what, what the
    Chief did about it, and the first challenge in its own words. No model
    writes it, so it claims nothing that was not said."""
    # Posts written before they kept their stage are given one from the order.
    said = [p for p in _staged(posts) if p.get("phase") == "huddle" and p.get("wave") == wave]
    counts: dict[str, dict[str, int]] = {}
    acts: list[str] = []
    first = None
    for item in said:
        author, kind = str(item.get("author_role")), str(item.get("kind"))
        if author == "chief":
            for pattern, phrase in CHIEF_DID:
                found = pattern.match(str(item.get("text") or ""))
                if found:
                    acts.append(phrase.format(found[1]))
            continue
        if kind == "challenge" and first is None:
            first = item
        counts.setdefault(author, {}).setdefault(kind, 0)
        counts[author][kind] += 1
    parts = []
    for author, kinds in counts.items():
        listed = [
            f"{kinds[kind]} {one if kinds[kind] == 1 else many}"
            for kind, one, many in SAID
            if kinds.get(kind)
        ]
        if listed:
            parts.append(f"{ROLE_TITLE.get(author, author.capitalize())}: " + ", ".join(listed))
    if not parts and not acts:
        return f"Huddle after wave {wave}: nothing raised."
    line = f"Huddle after wave {wave} — " + " · ".join(parts) + "."
    if acts:
        line += " Chief " + ", ".join(acts) + "."
    if first is not None:
        text = " ".join(str(first.get("text") or "").split())
        quoted = text if len(text) <= QUOTE else text[: QUOTE - 1].rstrip() + "…"
        who = ROLE_TITLE.get(str(first.get("author_role")), "")
        line += f" First challenge ({who}): “{quoted}”"
    return line
