from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.approvals import ApprovalScope
from app.db.models import Approval, Decision

ACTION = "engineering_scope"


class ApprovalStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    SUPERSEDED = "SUPERSEDED"


class ApprovalConflict(Exception):
    """The approval was already resolved, or the proposal has changed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ApprovalStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def open(self, run_id: UUID, task_id: UUID, scope: ApprovalScope) -> UUID:
        """Create the pending approval for a proposal.

        Replaying the approval node must not create a second pending record, so
        an existing pending approval for the same digest is reused.
        """
        digest = scope.digest()
        with self.session_factory() as session, session.begin():
            existing = session.execute(
                select(Approval).where(
                    Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.scope_digest == digest:
                    return existing.id
                # The proposal changed: the old one can no longer be approved.
                existing.status = ApprovalStatus.SUPERSEDED
                existing.resolved_at = datetime.now(UTC)
                session.flush()
            approval = Approval(
                run_id=run_id,
                task_id=task_id,
                action_type=ACTION,
                status=ApprovalStatus.PENDING,
                scope_json=scope.model_dump(mode="json"),
                scope_digest=digest,
            )
            session.add(approval)
            try:
                session.flush()
            except IntegrityError:
                raise ApprovalConflict("pending_approval_exists") from None
            return approval.id

    def supersede_pending(self, run_id: UUID) -> int:
        """Retire a run's pending approval (the run was cancelled). Kept, not deleted."""
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(Approval).where(
                        Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING
                    )
                )
            )
            for row in rows:
                row.status = ApprovalStatus.SUPERSEDED
                row.resolved_at = datetime.now(UTC)
            return len(rows)

    def pending(self) -> list[Approval]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(Approval)
                    .where(Approval.status == ApprovalStatus.PENDING)
                    .order_by(Approval.requested_at)
                )
            )

    def latest_status(self, run_id: UUID) -> str | None:
        """The most recent approval's status for a run, if it has one."""
        with self.session_factory() as session:
            return session.execute(
                select(Approval.status)
                .where(Approval.run_id == run_id)
                .order_by(Approval.requested_at.desc())
                .limit(1)
            ).scalar_one_or_none()

    def get(self, approval_id: UUID) -> Approval | None:
        with self.session_factory() as session:
            return session.get(Approval, approval_id)

    def resolve(
        self,
        approval_id: UUID,
        status: str,
        resolved_by: str,
        expected_digest: str,
        comment: str | None = None,
        revision_target: str | None = None,
        decision: dict[str, Any] | None = None,
    ) -> Approval:
        """Record one decision, exactly once.

        The transition is conditional on the approval still being pending, so a
        duplicated or concurrent decision cannot resolve it twice. The caller's
        digest must match the stored proposal, so a decision made against a
        stale brief cannot authorize newer work.
        """
        with self.session_factory() as session, session.begin():
            approval = session.get(Approval, approval_id)
            if approval is None:
                raise ApprovalConflict("unknown_approval")
            if approval.status != ApprovalStatus.PENDING:
                raise ApprovalConflict("already_resolved")
            if approval.scope_digest != expected_digest:
                raise ApprovalConflict("stale_scope")

            decision_id = None
            if decision is not None:
                record = Decision(
                    project_id=UUID(str(approval.scope_json["project_id"])),
                    title=decision["title"],
                    decision=decision["decision"],
                    reason=decision["reason"],
                    confidence=decision["confidence"],
                    approved_by=resolved_by,
                    revisit_condition=decision.get("revisit_condition"),
                    metadata_json={
                        "run_id": str(approval.run_id),
                        "scope_digest": approval.scope_digest,
                        "evidence": approval.scope_json.get("evidence", []),
                    },
                )
                session.add(record)
                session.flush()
                decision_id = record.id

            updated = session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == ApprovalStatus.PENDING)
                .values(
                    status=status,
                    resolved_at=func.now(),
                    resolved_by=resolved_by,
                    comment=comment,
                    revision_target=revision_target,
                    decision_id=decision_id,
                )
                .returning(Approval.id)
            ).scalar_one_or_none()
            if updated is None:
                raise ApprovalConflict("already_resolved")
            session.flush()
            session.refresh(approval)
            session.expunge(approval)
            return approval
