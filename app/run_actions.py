"""What the owner can do to a task, wherever they press it.

Stop, a note, help for a stuck step and rework were written inside the
office's routes. A button in Slack has to do exactly the same, with the same
rules about when each is allowed, so they live here and both call them. A
refusal is a code (`ActionRefused`), which the routes answer as HTTP and Slack
answers in words.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Run
from app.judgments import JudgmentUnavailable, build_judge
from app.office_settings import MonthlyLimitReached, OfficePaused
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.review import ReviewStore
from app.orchestrator.runner import RunRepository
from app.orchestrator.state import RunStatus
from app.project_settings import check_limits
from app.runtime.worker import RunWorker

# Owner control rules by run status.
INSTRUCTABLE = ("PENDING", "RUNNING", "RECOVERING", "PAUSED")
CANCELLABLE = (
    "PENDING",
    "RUNNING",
    "RECOVERING",
    "PAUSED",
    "WAITING_FOR_APPROVAL",
    "NEEDS_HUMAN_REVIEW",
)
# Finished tasks, which a rework reopens. The ones waiting on the owner are
# answered where they wait, not reworked.
REWORKABLE = ("COMPLETED", "FAILED", "CANCELLED")
# Below this, a judgment is treated as not knowing and the owner is asked.
REVISION_TARGET_CONFIDENCE = 0.6


class ActionRefused(Exception):
    """Why an action cannot be taken now, as a code and the HTTP status it means."""

    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def status_of(session_factory: sessionmaker[Session], run_id: UUID) -> str:
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ActionRefused("run_not_found", 404)
        return str(run.status)


def record_review(
    repository: RunRepository, run_id: UUID, action: str, comment: str, reviewer: str
) -> None:
    """Persist what a person decided, so the record shows who changed course."""
    described = repository.describe([run_id])
    if not described:
        return
    dispatch, _, _ = described[0]
    repository.record_stage(
        dispatch,
        "human_review",
        {"action": action, "comment": comment, "reviewed_by": reviewer},
    )


def rework_note(state: dict[str, Any]) -> str:
    """What a rework with no note from the owner should improve: what
    the review raised, what the answer said it could not check, how it reads."""
    critique = dict(state.get("last_critique") or state.get("critique") or {})
    answer = dict(state.get("synthesis") or {})
    lines = ["Do this again and make it better. Keep what was right."]
    issues = [
        f"- {item.get('issue')} (fix: {item.get('fix')})"
        for item in critique.get("issues") or []
        if isinstance(item, dict) and item.get("issue")
    ] or [f"- {item}" for item in (critique.get("critical_issues") or [])[:5]]
    if issues:
        lines.append("Fix what the review raised:\n" + "\n".join(issues))
    gaps = [str(answer.get("not_checked") or "").strip()] + [
        str(item) for item in (answer.get("unknowns") or [])[:5]
    ]
    gaps = [gap for gap in gaps if gap]
    if gaps:
        lines.append(
            "Fill these gaps where the evidence allows:\n" + "\n".join(f"- {g}" for g in gaps)
        )
    fixes = [str(item) for item in critique.get("writing_fixes") or []]
    if fixes:
        lines.append("Write it better:\n" + "\n".join(f"- {f}" for f in fixes))
    if len(lines) == 1:
        lines.append(
            "Find what is missing or weak for the person who asked, look for better and more "
            "current evidence, and make the answer clearer and more useful to act on."
        )
    return "\n\n".join(lines)[:8000]


class RunActions:
    """The owner's actions on a task, with the office's rules for each."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: RunRepository,
        worker: RunWorker,
        approvals: ApprovalStore,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.repository = repository
        self.worker = worker
        self.approvals = approvals
        self.settings = settings

    def control(
        self, run_id: UUID, kind: str, text: str | None = None, target: str | None = None
    ) -> dict[str, Any]:
        """Instruct, pause, cancel or skip.

        On a running run a control waits for the next step boundary. A run that
        is not executing is paused or cancelled at once; cancelling a run that
        waits for approval also retires that approval.
        """
        status = status_of(self.session_factory, run_id)
        if kind in ("instruction", "skip", "prioritize") and status not in INSTRUCTABLE:
            raise ActionRefused("run_not_accepting_instructions")
        if kind == "pause" and status not in ("PENDING", "RUNNING", "RECOVERING"):
            raise ActionRefused("run_not_pausable")
        if kind == "cancel" and status not in CANCELLABLE:
            raise ActionRefused("run_not_cancellable")
        control = self.repository.add_control(run_id, kind, text, target)
        if kind in ("instruction", "skip", "prioritize"):
            return control
        stop = RunStatus.CANCELLED if kind == "cancel" else RunStatus.PAUSED
        # Only where no worker is executing it; otherwise the boundary applies it.
        idle = [s for s in (CANCELLABLE if kind == "cancel" else ("PENDING",)) if s != "RUNNING"]
        if status != "RUNNING" and self.repository.stop(run_id, stop, only_from=tuple(idle)):
            self.repository.apply_stop_controls(run_id)
            if stop == RunStatus.CANCELLED:
                self.approvals.supersede_pending(run_id)
        return next(c for c in self.repository.controls(run_id) if c["id"] == control["id"])

    def help(self, run_id: UUID, action: str, text: str | None = None) -> dict[str, Any]:
        """The owner's answer to a stuck task: a hint the team follows
        from here, skipping the step it is stuck on, or trying again."""
        if status_of(self.session_factory, run_id) != RunStatus.PAUSED:
            raise ActionRefused("run_not_paused")
        if self.repository.binding_ceiling(run_id, self.settings.llm_max_output_tokens):
            raise ActionRefused("raise_the_limit_first")
        stuck = self.repository.pending_stuck_ask(run_id) or {}
        if action == "hint":
            self.repository.add_control(run_id, "instruction", (text or "").strip())
        elif action == "skip":
            if not stuck.get("step"):
                raise ActionRefused("nothing_to_skip")
            self.repository.add_control(run_id, "skip", str(stuck["step"]))
        self.repository.add_event(
            run_id, "help_given", {"action": action, "step": stuck.get("step")}
        )
        if not self.worker.resume_paused(run_id):
            raise ActionRefused("run_not_paused")
        return {"status": "PENDING", "action": action}

    def reopen(self, run_id: UUID, comment: str, target: str | None) -> str | None:
        """Send a stopped or finished task back to work; returns the part redone.

        With a plan, as a revision through the critic's edges. Without one, the
        task starts again from planning, the note given to every step as the
        owner's direction: there is nothing to revise against.
        """
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ActionRefused("run_not_found", 404)
            planned = bool((run.state_json or {}).get("plan"))
        reviews = ReviewStore(self.session_factory)
        if not planned:
            reviews.restart(run_id, self.settings.owner_name)
            if comment.strip():
                self.repository.add_control(run_id, "instruction", comment.strip())
            self.worker.restart(run_id)
            return "plan"
        override = reviews.start_cycle(run_id, comment, target, self.settings.owner_name)
        self.worker.resume_for_review(run_id, override)
        redone = override["critique"]["revision_target"]
        return str(redone) if redone else None

    async def judged_target(self, note: str) -> tuple[str, str]:
        """Pick research or strategy from the owner's note, or refuse to guess.

        Without a judge the historical default (research) applies. With one,
        an unclear note or an unconfident answer is sent back to the owner.
        """
        judge = build_judge(self.settings)
        if judge is None:
            return "research", "default"
        try:
            choice, confidence = await judge.revision_target(note)
        except JudgmentUnavailable:
            raise ActionRefused("revision_target_unavailable", 503) from None
        if choice not in ("research", "strategy") or confidence < REVISION_TARGET_CONFIDENCE:
            raise ActionRefused("revision_target_ambiguous", 422)
        return choice, f"judged ({confidence:.2f})"

    async def rework(
        self, run_id: UUID, comment: str | None = None, target: str | None = None
    ) -> str | None:
        """Redo part of a finished task, in the same task.

        The owner's note becomes the revision the critic would have asked for,
        and the named step and everything after it run again. Each artifact is
        a new version, so the earlier answer stays. It is new work, so the
        office's and the project's limits apply.
        """
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ActionRefused("run_not_found", 404)
            if run.status not in REWORKABLE:
                raise ActionRefused("not_finished")
            try:
                check_limits(session, self.settings, run.project_id)
            except OfficePaused:
                raise ActionRefused("office_paused") from None
            except MonthlyLimitReached:
                raise ActionRefused("monthly_limit_reached") from None
            state = dict(run.state_json or {})
        # No note: the office says what to improve, from the task's own record.
        note = comment or rework_note(state)
        chosen_by = ""
        if target is None:
            target, chosen_by = await self.judged_target(note)
        record_review(self.repository, run_id, "rework", note, self.settings.owner_name)
        redone = self.reopen(run_id, note, target)
        self.repository.add_event(
            run_id, "rework_requested", {"target": redone, "chosen_by": chosen_by or "owner"}
        )
        return redone
