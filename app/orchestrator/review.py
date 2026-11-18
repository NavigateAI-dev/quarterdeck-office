from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app import run_state
from app.db.models import Artifact, Run
from app.orchestrator.state import RunStatus

# A human revision is expressed as a critique, so it routes through the same
# validated edges as an automated one. The workflow has one revision path.
HUMAN_CRITIC = "human"


class ReviewRejected(Exception):
    """The run is not in a state a person can act on."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


REVIEWABLE = {RunStatus.NEEDS_HUMAN_REVIEW, RunStatus.BLOCKED, RunStatus.FAILED}
# A task can be finished from a review stop or from a limit it reached.
FINISHABLE = REVIEWABLE | {RunStatus.PAUSED}
# The documents an answer can be written from.
WORK_KINDS = ("research_report", "strategy_proposal")


def has_work(state: dict[str, Any]) -> bool:
    """Whether anything was researched or written that an answer can be made from."""
    return bool(state.get("research") or state.get("strategy"))


def human_critique(comment: str, target: str | None) -> dict[str, Any]:
    """Express a person's decision in the Critic's contract.

    A human decision routes through the same validated edges as an automated
    one, so the workflow has a single revision path rather than two.
    """
    if target is None:
        return {
            "verdict": "PASS",
            "revision_target": None,
            "required_corrections": [],
            "critical_issues": [],
            "weak_assumptions": [],
            "missing_evidence": [],
            "alternative_explanations": [],
            "what_would_change_conclusion": [],
            "confidence": "high",
        }
    return {
        "verdict": "REVISE",
        "revision_target": target,
        "required_corrections": [comment],
        "critical_issues": [],
        "weak_assumptions": [],
        "missing_evidence": [],
        "alternative_explanations": [],
        "what_would_change_conclusion": [],
        "confidence": "high",
    }


class ReviewStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def load(self, run_id: UUID) -> Run:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ReviewRejected("unknown_run")
            if run.status not in REVIEWABLE:
                raise ReviewRejected("run_not_awaiting_review")
            session.expunge(run)
            return run

    def start_cycle(
        self, run_id: UUID, comment: str, target: str | None, reviewer: str
    ) -> dict[str, Any]:
        """Begin a new human review cycle.

        A target sends the work back for revision. No target accepts the
        analysis as it stands and lets it proceed to synthesis and the approval
        gate: the Critic advises, the owner decides. Accepting analysis
        authorizes no engineering; that still requires an explicit approval
        bound to a scope digest.

        Returns the state the checkpoint must be given. The checkpoint is
        authoritative for continuation, so a reset written only to the domain
        row is ignored: the resumed graph would still see the spent revision
        budget and stop again immediately.

        The automated budget resets because this is a new cycle with new
        instructions, and each cycle stays bounded by the same limit.
        """
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                raise ReviewRejected("unknown_run")
            state = dict(run.state_json or {})
            if not state.get("research"):
                # Stopped before anything was researched, as a plan waiting for
                # the owner is. Accepting routes to the summary, which would
                # answer from the plan alone: the work goes to research, with
                # whatever the owner said as its direction.
                target = "research"
                comment = comment.strip() or "Research the plan as it stands."
            override: dict[str, Any] = {
                "critique": human_critique(comment, target),
                "last_critique": None,
                "revision_count": 0,
                "human_cycles": int(state.get("human_cycles", 0)) + 1,
                "stopped_reason": None,
                "status": RunStatus.RUNNING,
            }
            state.update(override, reviewed_by=reviewer)
            run_state.write(session, run, run_state.REPLACE, state, "review")
            # PENDING, not RUNNING: the worker claims a run by transitioning it
            # out of PENDING, so marking it RUNNING here makes the claim fail
            # and the resumed work is silently dropped.
            session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    status="PENDING",
                    error=None,
                    completed_at=None,
                    current_node=None,
                )
            )
            return override

    def finish(self, run_id: UUID, reviewer: str) -> dict[str, Any]:
        """Write the answer from the work done so far, and nothing more.

        Closing a task used to keep nothing: research paid for was left
        unread. Finishing accepts the work as it stands, so the critique
        routes straight to the answer: no more research, no replanning.
        """
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                raise ReviewRejected("unknown_run")
            if run.status not in FINISHABLE:
                raise ReviewRejected("run_not_finishable")
            state = dict(run.state_json or {})
            # A paused task's work is in its checkpoint and its documents; the
            # record's state is only written in full when a task ends.
            kept = session.scalar(
                select(Artifact.id)
                .where(Artifact.run_id == run_id, Artifact.type.in_(WORK_KINDS))
                .limit(1)
            )
            if not has_work(state) and kept is None:
                raise ReviewRejected("nothing_to_finish")
            override: dict[str, Any] = {
                "critique": human_critique("", None),
                "last_critique": None,
                "human_cycles": int(state.get("human_cycles", 0)) + 1,
                "stopped_reason": None,
                "finishing": True,
                "status": RunStatus.RUNNING,
            }
            state.update(override, reviewed_by=reviewer)
            run_state.write(session, run, run_state.REPLACE, state, "review")
            session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    status="PENDING",
                    error=None,
                    completed_at=None,
                    current_node=None,
                )
            )
            return override

    def restart(self, run_id: UUID, reviewer: str) -> None:
        """Start a run again from its plan, keeping only what it was told.

        For a run that stopped before it had a plan: there is no research or
        strategy to revise against, and resuming as a revision would ask the
        research step to read a plan that does not exist.
        """
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                raise ReviewRejected("unknown_run")
            state = dict(run.state_json or {})
            fresh = {
                "context": state.get("context") or {},
                "human_cycles": int(state.get("human_cycles", 0)) + 1,
                "reviewed_by": reviewer,
            }
            run_state.write(session, run, run_state.REPLACE, fresh, "review")
            session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    status="PENDING",
                    error=None,
                    completed_at=None,
                    current_node=None,
                )
            )

    def close(self, run_id: UUID, comment: str, reviewer: str) -> None:
        with self.session_factory() as session, session.begin():
            session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(status=RunStatus.CLOSED, error=f"closed_by_{reviewer}: {comment[:200]}")
            )
