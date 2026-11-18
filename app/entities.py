"""The recurring subjects of the work, and the dossiers they accumulate.

A business connects the same dots repeatedly: the Wwft, DNB, eIDAS 2.0, a
vendor, a brand. This office rediscovered each of them on every task, and once
got the Wwft's own identifier wrong and read the Criminal Code for forty
thousand characters.

So: the owner names the subjects that matter, because they know their domain in
a way no extraction does. The office attaches what it learns about them, by
matching the name it was given in finished work — deterministic, not inferred.
Jev then says how durable each candidate looks, and the owner keeps or discards
it, exactly as with memory.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

# A fact is one line a later task can use. Longer than this is a report.
FACT_LIMIT = 300
EVIDENCE_LIMIT = 400
MAX_FACTS_PER_RUN = 24
# Per subject, so one that is mentioned on every line cannot starve the rest.
# The Wwft filled a whole run's allowance and left DNB, the EBA and eIDAS with
# nothing, which is how a dossier ends up knowing one thing well and the rest
# not at all.
MAX_FACTS_PER_ENTITY = 6
# A dossier is a reminder, not an archive.
DOSSIER_LIMIT = 8
# Split only where a sentence really ends: a stop followed by a space, or a
# CJK stop. Splitting on every period would break "eIDAS 2.0",
# "wetten.overheid.nl" and "art. 3" — which are the things this work is about.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])|\n+")


@dataclass(frozen=True)
class FactCandidate:
    entity: str
    fact: str
    evidence: str


def digest_of(fact: str) -> str:
    return hashlib.sha256(fact.strip().lower().encode()).hexdigest()


def names_of(entity: Any) -> list[str]:
    """Everything this subject is called, longest first.

    Longest first so "eIDAS 2.0" matches before "eIDAS" and the more specific
    subject wins.
    """
    found = [str(entity.name), *[str(a) for a in entity.aliases_json or []]]
    return sorted({n.strip() for n in found if n and n.strip()}, key=len, reverse=True)


def mentions(text: str, names: list[str]) -> bool:
    """Whether this text is about the subject at all."""
    lowered = text.lower()
    return any(name.lower() in lowered for name in names)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_END.split(text or "") if part and part.strip()]


def facts_from(report: dict[str, Any], entities: list[Any]) -> list[FactCandidate]:
    """What a finished run said about each named subject.

    Taken from the run's own findings and answers, sentence by sentence. The
    office proposes what the work said; it does not write something new about a
    regulator and attribute it to the work.
    """
    lines: list[str] = []
    for finding in report.get("key_findings") or []:
        lines.extend(_sentences(str(finding)))
    for answer in report.get("answers") or []:
        if isinstance(answer, dict):
            lines.extend(_sentences(str(answer.get("answer") or "")))
    for item in report.get("evidence") or []:
        lines.extend(_sentences(str(item)))

    per_entity: list[list[FactCandidate]] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        names = names_of(entity)
        mine: list[FactCandidate] = []
        for line in lines:
            if len(mine) >= MAX_FACTS_PER_ENTITY:
                break
            if len(line) < 20 or not mentions(line, names):
                continue
            fact = " ".join(line.split())[:FACT_LIMIT]
            key = (str(entity.name), digest_of(fact))
            if key in seen:
                continue
            seen.add(key)
            mine.append(
                FactCandidate(
                    entity=str(entity.name),
                    fact=fact,
                    evidence=f"Said by a finished task about {entity.name}."[:EVIDENCE_LIMIT],
                )
            )
        per_entity.append(mine)

    # Taken a turn at a time, so the run's allowance is shared between the
    # subjects rather than spent on whichever came first.
    found: list[FactCandidate] = []
    for index in range(MAX_FACTS_PER_ENTITY):
        for mine in per_entity:
            if index < len(mine) and len(found) < MAX_FACTS_PER_RUN:
                found.append(mine[index])
    return found


def about(objective: str, entities: list[Any]) -> list[Any]:
    """The subjects this task is about, so it is told those dossiers and no others.

    A task about the Wwft should not be handed everything the office knows
    about every brand it has ever researched.
    """
    return [entity for entity in entities if mentions(objective, names_of(entity))]
