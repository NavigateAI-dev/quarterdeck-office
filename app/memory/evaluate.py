"""Evaluate one finished run into candidate lessons.

Nothing here runs by itself. There is no hook, no completion trigger and no
schedule: the owner names a run. A dry run shows exactly what would be sent
and sends nothing, which is the only way to be sure what leaves the machine.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.judgments import Judge, JudgmentUnavailable
from app.memory.candidates import Candidate, candidates_for


class NothingLearned(Exception):
    """The run's record supports no reusable lesson."""


def projection(candidates: list[Candidate]) -> list[dict[str, str]]:
    """Exactly what would be sent to the evaluator, and nothing else.

    One line per lesson with the trace fact behind it, already redacted. No
    page bodies, no artifacts, no prompts, no credentials.
    """
    return [{"kind": c.kind, "lesson": c.lesson, "evidence": c.evidence} for c in candidates]


async def evaluate(
    report: dict[str, Any],
    critique: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    stopped: str,
    project_name: str,
    judge: Judge | None,
    dry_run: bool = False,
) -> tuple[list[Candidate], list[float], str | None]:
    """Candidates for one run, with Jev's ranking unless this is a dry run.

    Returns the candidates, their probabilities (empty when not evaluated) and
    the evaluator that produced them.
    """
    found = candidates_for(report, critique, usage, stopped)
    if not found:
        raise NothingLearned("no_candidates")
    if dry_run or judge is None:
        return found, [], None
    try:
        probabilities = await judge.reusable([c.lesson for c in found], project_name)
    except JudgmentUnavailable:
        # Ranking is a convenience; the owner can still review unranked.
        return found, [], None
    return found, probabilities, "jev-latest"


def trace_of(
    repository: Any, run_id: UUID, artifacts: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """The parts of a run's record a lesson can be drawn from."""
    report = artifacts.get("research_report") or {}
    critique = artifacts.get("critique") or {}
    usage = repository.usage(run_id)
    stopped = "budget" if repository.pending_budget_ask(run_id) else ""
    return report, critique, usage, stopped
