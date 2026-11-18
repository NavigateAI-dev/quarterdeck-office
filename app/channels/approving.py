"""Approving from a Slack card: the owner's button on a low-risk proposal.

Only a proposal whose approval records a decision and nothing more
(`app.approvals.low_risk`) is decided here. It is read again from the stored
approval rather than trusted from the button; the button's digest must still
be the proposal's; and an approval is decided once (`ApprovalStore.resolve`).
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.approvals import ApprovalScope, approval_decision, low_risk
from app.channels import slack_format
from app.orchestrator.approval_store import ApprovalConflict, ApprovalStatus, ApprovalStore

# Who decided, as an approval records it.
FROM_SLACK = "owner (Slack)"
DECIDED = {
    "already_resolved": "That approval was already decided.",
    "stale_scope": "The proposal changed since this message. Read the new one in the office.",
    "unknown_approval": "That approval is no longer in the office.",
}


class Resumes(Protocol):
    def resume(self, run_id: UUID) -> object: ...


def decide(approvals: ApprovalStore, worker: Resumes, action: str, value: str) -> tuple[bool, str]:
    """Whether the approval was decided, and what to say to the owner."""
    approval_id, _, digest = value.partition(":")
    try:
        approval = approvals.get(UUID(approval_id))
    except ValueError:
        approval = None
    if approval is None:
        return False, DECIDED["unknown_approval"]
    scope = ApprovalScope.model_validate(approval.scope_json)
    if not low_risk(scope):
        return False, "This one is approved in the office, where you can read all of it."
    approve = action == slack_format.APPROVE
    try:
        resolved = approvals.resolve(
            approval.id,
            ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED,
            resolved_by=FROM_SLACK,
            expected_digest=digest,
            decision=approval_decision(scope, "Approved in Slack.") if approve else None,
        )
    except ApprovalConflict as conflict:
        return False, DECIDED.get(conflict.reason, "I could not do that: " + conflict.reason)
    worker.resume(resolved.run_id)
    return True, "✓ Approved by you in Slack." if approve else "Rejected by you in Slack."
