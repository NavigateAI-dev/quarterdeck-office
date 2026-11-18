"""What a finished run learned, as candidate lessons.

Beacon's cross-harness memory turns reviewed traces into project memory that
later agents reuse. The office already keeps the trace: a run records its plan,
which questions it answered, which sources refused it, what the review raised,
and how it stopped. Until now all of that was thrown away when the run ended,
so the next task rediscovered that a regulator's site returns 403 and that a
law is at one address rather than another.

Candidates are derived from those recorded facts, not written by a model. Jev
ranks and classifies them afterwards; it does not author them. That keeps the
lesson traceable to something the run actually observed, and keeps what leaves
the machine small: a candidate is one sentence and its evidence, never a page
body, an artifact, or a secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A lesson is one line a future task can act on. Anything longer is a report.
LESSON_LIMIT = 300
EVIDENCE_LIMIT = 400
MAX_CANDIDATES = 24
# Never carried out of the office, whatever a payload happens to hold.
SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}"
    r"|secret:[A-Za-z0-9_-]+|[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    """One reusable lesson, and the trace fact that supports it."""

    kind: str
    lesson: str
    evidence: str

    def redacted(self) -> Candidate:
        return Candidate(
            kind=self.kind,
            lesson=redact(self.lesson)[:LESSON_LIMIT],
            evidence=redact(self.evidence)[:EVIDENCE_LIMIT],
        )


def redact(text: str) -> str:
    """Remove anything shaped like a credential before it can travel."""
    return SECRET.sub("[redacted]", " ".join(str(text).split()))


def _blocked(report: dict[str, Any]) -> list[Candidate]:
    found = []
    for source in report.get("blocked_sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        status = str(source.get("status") or "")
        if not url:
            continue
        if status == "not_the_document_named":
            found.append(
                Candidate(
                    kind="wrong_address",
                    lesson=f"{url} is not the document it was named as; find the right address.",
                    evidence=f"The run retrieved {url} and it was about something else.",
                )
            )
        else:
            found.append(
                Candidate(
                    kind="source_blocked",
                    lesson=f"{url} cannot be read by the office ({status}); use another route.",
                    evidence=f"The run requested {url} and it answered {status}.",
                )
            )
    return found


def _unanswered(report: dict[str, Any]) -> list[Candidate]:
    found = []
    for answer in report.get("answers") or []:
        if not isinstance(answer, dict) or str(answer.get("status")) == "answered":
            continue
        question = str(answer.get("question") or "").strip()
        missing = [str(item) for item in answer.get("missing") or []]
        if not question:
            continue
        found.append(
            Candidate(
                kind="unanswered",
                lesson=(
                    f"Public sources did not answer: {question} "
                    + (f"What was missing: {missing[0]}" if missing else "")
                ).strip(),
                evidence=str(answer.get("answer") or "")[:EVIDENCE_LIMIT],
            )
        )
    return found


def _review(critique: dict[str, Any]) -> list[Candidate]:
    found = []
    for field in ("critical_issues", "required_corrections"):
        for issue in critique.get(field) or []:
            found.append(
                Candidate(
                    kind="review_issue",
                    lesson=f"The review raised, and it was not resolved: {issue}",
                    evidence=f"Recorded by the critic as {field.replace('_', ' ')}.",
                )
            )
    return found


def _limits(usage: dict[str, Any], stopped: str) -> list[Candidate]:
    spent = usage.get("spent") or {}
    budget = usage.get("budget") or {}
    tokens, allowed = spent.get("tokens"), budget.get("max_tokens")
    if stopped == "budget" and tokens and allowed:
        return [
            Candidate(
                kind="budget",
                lesson=(
                    f"A task of this kind reached its token limit at {int(allowed):,}; "
                    f"allow more than {int(tokens):,} for the next one."
                ),
                evidence=f"The run spent {int(tokens):,} of {int(allowed):,} tokens.",
            )
        ]
    return []


def candidates_for(
    report: dict[str, Any],
    critique: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    stopped: str = "",
) -> list[Candidate]:
    """Every lesson this run's own record supports, redacted and deduplicated."""
    found = [
        *_blocked(report),
        *_unanswered(report),
        *_review(critique or {}),
        *_limits(usage or {}, stopped),
    ]
    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in found:
        cleaned = candidate.redacted()
        if not cleaned.lesson or cleaned.lesson in seen:
            continue
        seen.add(cleaned.lesson)
        unique.append(cleaned)
    return unique[:MAX_CANDIDATES]
