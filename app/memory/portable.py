"""An approved lesson as an Agent Skill, and back again.

Beacon's cross-harness memory keeps what an agent learned in a place no single
harness owns: `.agents/skills/<slug>/SKILL.md`, a file with front matter that
any harness can read. This office already learns — a source that answers 403, an
address that turns out to be the wrong law — and until now that stayed here.

What leaves is what the owner approved and nothing else: the lesson, its
evidence, and where it came from. No project identifiers from this installation,
no run bodies, no keys. An import is a proposal, not a state to trust: a lesson
arriving without provenance is refused rather than believed, because a memory
with no traceable origin is a rumour, and the whole point of the review gate is
that the owner can see what a claim rests on.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.memory.candidates import EVIDENCE_LIMIT, LESSON_LIMIT, Candidate, redact

FORMAT = 1
SKILL_ROOT = ".agents/skills"
# Front matter keys that must be present and non-empty for an import to be
# considered traceable. "learned_by" names the harness, not this office.
REQUIRED_PROVENANCE = ("learned_by", "learned_at")
SLUG_LIMIT = 48
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class NoProvenance(ValueError):
    """A lesson that cannot say where it came from."""


def slug_for(lesson: str, kind: str) -> str:
    """A stable directory name, readable by a person reading a file tree."""
    words = _NOT_SLUG.sub("-", f"{kind} {lesson}".lower()).strip("-")
    if len(words) > SLUG_LIMIT:
        words = words[:SLUG_LIMIT].rsplit("-", 1)[0] or words[:SLUG_LIMIT]
    return words or "lesson"


def to_skill(row: Any, office: str = "Quarterdeck") -> dict[str, str]:
    """One approved lesson as a SKILL.md, with the path it belongs at.

    Takes the stored row so the export carries what was actually decided —
    which run produced it, which evaluator ranked it and how highly — rather
    than a retelling.
    """
    lesson = redact(str(row.lesson or ""))[:LESSON_LIMIT]
    evidence = redact(str(row.evidence or ""))[:EVIDENCE_LIMIT]
    slug = slug_for(lesson, str(row.kind or "lesson"))
    decided = getattr(row, "decided_at", None) or datetime.now(UTC)
    front: dict[str, Any] = {
        "name": slug,
        "description": lesson,
        "format": FORMAT,
        "kind": str(row.kind or "lesson"),
        "learned_by": office,
        "learned_at": decided.isoformat(),
        "learned_in_run": str(row.run_id) if getattr(row, "run_id", None) else None,
        "evaluator": getattr(row, "evaluator", None),
        "reusable_probability": getattr(row, "reusable_probability", None),
        "scope": getattr(row, "scope", "project"),
    }
    body = [
        f"# {lesson}",
        "",
        "## What to do with this",
        "",
        lesson,
        "",
    ]
    if evidence:
        body += ["## What it rests on", "", evidence, ""]
    body += [
        "## Where it came from",
        "",
        f"Learned by {office}"
        + (f" in run {front['learned_in_run']}" if front["learned_in_run"] else "")
        + f", approved by its owner on {decided.date().isoformat()}.",
        "",
    ]
    return {
        "path": f"{SKILL_ROOT}/{slug}/SKILL.md",
        "text": _front_matter(front) + "\n".join(body),
    }


def read_skill(text: str) -> dict[str, Any]:
    """A SKILL.md from any harness, as a candidate this office could keep.

    Raises NoProvenance when the file cannot say who learned it and when. The
    office would rather import nothing than import a claim it cannot trace.
    """
    match = _FRONT.match(text.strip() + "\n")
    if match is None:
        raise NoProvenance("no front matter")
    front = _read_front_matter(match.group(1))
    missing = [key for key in REQUIRED_PROVENANCE if not str(front.get(key) or "").strip()]
    if missing:
        raise NoProvenance(", ".join(missing))
    lesson = str(front.get("description") or "").strip()
    if not lesson:
        # Some harnesses put the lesson only in the body's first heading.
        for line in match.group(2).splitlines():
            if line.startswith("# "):
                lesson = line[2:].strip()
                break
    if not lesson:
        raise NoProvenance("no lesson")
    return {
        "candidate": Candidate(
            kind=str(front.get("kind") or "imported"),
            lesson=redact(lesson)[:LESSON_LIMIT],
            evidence=redact(_section(match.group(2), "What it rests on"))[:EVIDENCE_LIMIT],
        ),
        "learned_by": str(front["learned_by"]).strip(),
        "learned_at": str(front["learned_at"]).strip(),
        "learned_in_run": str(front.get("learned_in_run") or "").strip() or None,
        "evaluator": str(front.get("evaluator") or "").strip() or None,
    }


def _section(body: str, heading: str) -> str:
    """The text under one `## heading`, up to the next one."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == f"## {heading}".lower():
            rest = []
            for following in lines[index + 1 :]:
                if following.startswith("## "):
                    break
                rest.append(following)
            return "\n".join(rest).strip()
    return ""


def _front_matter(values: dict[str, Any]) -> str:
    """Written by hand rather than through YAML: the values are scalars, and a
    skill file another harness reads should be predictable to the character."""
    lines = ["---"]
    for key, value in values.items():
        if value is None or value == "":
            continue
        if isinstance(value, float):
            lines.append(f"{key}: {value:.2f}")
        else:
            lines.append(f"{key}: {_scalar(str(value))}")
    lines += ["---", ""]
    return "\n".join(lines)


def _scalar(value: str) -> str:
    one_line = " ".join(value.split())
    if any(character in one_line for character in ':#"') or one_line != value:
        return '"' + one_line.replace('"', "'") + '"'
    return one_line


def _read_front_matter(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip().strip("\"'")
    return values
