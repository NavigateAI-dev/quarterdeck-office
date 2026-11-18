"""The reviewer's reading of a coding task's change, kept on its report (36c)."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentOutputError, BaseAgent
from app.approvals import ApprovalScope
from app.schemas.engineering import EngineeringStatus

# Enough of a diff to read; the whole patch is kept on the office's side.
DIFF_CHARS = 60_000


def review_inputs(scope: ApprovalScope, report: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {item}" for item in scope.acceptance_criteria) or "- (none named)"
    tests = report.get("tests") or {}
    diff = str(report.get("diff") or "")
    cut = len(diff) > DIFF_CHARS or bool(report.get("diff_truncated"))
    return (
        f"The task:\n{scope.proposed_engineering_scope}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"The office's checks ({scope.test_command or 'none'}): {tests.get('status')}\n"
        f"{str(tests.get('details') or '')[-1500:]}\n\n"
        f"Files changed: {', '.join(report.get('changed_files') or []) or 'none'}\n\n"
        f"The diff{' (cut short)' if cut else ''}:\n{diff[:DIFF_CHARS]}"
    )


async def reviewed(
    reviewer: BaseAgent[Any] | None,
    scope: ApprovalScope,
    report: dict[str, Any],
    context: Any,
) -> dict[str, Any] | None:
    """The reviewer's findings, or None when there is nothing to review. A
    reviewer that fails leaves the work as it is and says so."""
    if reviewer is None or report.get("status") != EngineeringStatus.COMPLETED:
        return None
    if not report.get("diff"):
        return None
    try:
        output = await reviewer.run(scope.objective, context, review_inputs(scope, report))
    except AgentOutputError as failed:
        if str(failed.code).startswith("budget_exhausted"):
            raise
        return {"verdict": "not_reviewed", "findings": [], "plain": "The reviewer did not answer."}
    return dict(output.model_dump(mode="json"))
