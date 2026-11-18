"""Running the office's policies against real work, and recording what happened."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Policy, PolicyRecord, PolicyState
from app.policies import builtin  # noqa: F401 - registers the built-in policies
from app.policies.engine import (
    REGISTRY,
    ConfiguredPolicy,
    Decision,
    Hook,
    PolicyEvent,
    Verdict,
    evaluate,
)


class PolicyRefused(Exception):
    """A policy refused this step. The reason is the owner's, not a stack trace."""

    def __init__(self, decision: Decision, record_id: int | None = None) -> None:
        self.decision = decision
        self.record_id = record_id
        super().__init__(decision.reason or "refused by a policy")


class PolicyAsk(PolicyRefused):
    """A policy wants the owner's word before this step may run."""


def configured(session: Session, project_id: UUID | None) -> list[ConfiguredPolicy]:
    """This project's policies first, then the office's."""
    rows = list(
        session.scalars(
            select(Policy).where(Policy.enabled.is_(True)).order_by(Policy.scope, Policy.created_at)
        )
    )
    chosen = [
        row
        for row in rows
        if row.scope == "office" or (project_id is not None and row.project_id == project_id)
    ]
    chosen.sort(key=lambda row: 0 if row.scope == "project" else 1)
    return [
        ConfiguredPolicy(
            id=row.id,
            kind=row.kind,
            scope=row.scope,
            params=dict(row.params_json or {}),
            project_id=row.project_id,
        )
        for row in chosen
    ]


def counters(session: Session, run_id: UUID | None) -> dict[str, Any]:
    if run_id is None:
        return {}
    row = session.get(PolicyState, run_id)
    return dict(row.counters_json or {}) if row else {}


class PolicyGate:
    """The office's rules, as the run sees them."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        # The record written for the most recent decision, so a pause can
        # point the owner at the exact question.
        self.last_record_id: int | None = None

    def check(
        self,
        hook: Hook,
        target: str,
        run_id: UUID | None = None,
        project_id: UUID | None = None,
        **data: Any,
    ) -> Decision:
        """Ask the policies, record the answer, and keep any counters."""
        with self.session_factory() as session:
            policies = configured(session, project_id)
            state = counters(session, run_id)
        if not policies:
            return Decision(Verdict.ALLOW)
        event = PolicyEvent(
            hook=hook,
            target=target,
            run_id=run_id,
            project_id=project_id,
            data=data,
            state=state,
        )
        decision = evaluate(event, policies)
        self._record(event, decision)
        return decision

    def enforce(self, *args: Any, **kwargs: Any) -> Decision:
        """Like check, but a refusal stops the step and an ask pauses the task."""
        decision, record_id = self.check_with_record(*args, **kwargs)
        if decision.verdict is Verdict.ASK:
            raise PolicyAsk(decision, record_id)
        if decision.verdict is Verdict.DENY:
            raise PolicyRefused(decision, record_id)
        return decision

    def ask_owner(
        self,
        subject: str,
        reason: str,
        target: str,
        run_id: UUID | None = None,
        project_id: UUID | None = None,
        **data: Any,
    ) -> None:
        """A question only the owner answers, asked once per task.

        Not a configured policy: the owner already said, when reviewing, that
        this is theirs to decide. It is asked through the same record as a
        policy's question, so it is answered in the same place and an allow
        lasts for this task only. Returns when the owner has allowed it.
        """
        with self.session_factory() as session, session.begin():
            if counters(session, run_id).get(f"allow:{subject}"):
                return
            # Parallel subtasks can ask the same thing at once; one question is
            # enough, and a second open copy would outlive the answer.
            record = session.scalar(
                select(PolicyRecord).where(
                    PolicyRecord.run_id == run_id,
                    PolicyRecord.policy == subject,
                    PolicyRecord.verdict == str(Verdict.ASK),
                    PolicyRecord.decided_at.is_(None),
                )
            )
            if record is None:
                record = PolicyRecord(
                    run_id=run_id,
                    project_id=project_id,
                    hook=str(Hook.TOOL_CALL),
                    target=target,
                    verdict=str(Verdict.ASK),
                    policy=subject,
                    reason=reason,
                    detail_json={
                        key: value
                        for key, value in data.items()
                        if isinstance(value, str | int | float | bool)
                    },
                )
                session.add(record)
                session.flush()
            self.last_record_id = record.id
        raise PolicyAsk(Decision(Verdict.ASK, reason, subject), self.last_record_id)

    def check_with_record(self, *args: Any, **kwargs: Any) -> tuple[Decision, int | None]:
        decision = self.check(*args, **kwargs)
        return decision, self.last_record_id

    def _record(self, event: PolicyEvent, decision: Decision) -> None:
        with self.session_factory() as session, session.begin():
            if decision.add and event.run_id is not None:
                merged = {**counters(session, event.run_id)}
                for key, value in decision.add.items():
                    merged[key] = int(merged.get(key, 0)) + int(value)
                session.execute(
                    insert(PolicyState)
                    .values(run_id=event.run_id, counters_json=merged)
                    .on_conflict_do_update(
                        index_elements=[PolicyState.run_id], set_={"counters_json": merged}
                    )
                )
            # An allowed step is recorded only when a policy had something to
            # say, so the record reads as decisions rather than as traffic.
            if decision.verdict is Verdict.ALLOW and not decision.reason:
                self.last_record_id = None
                return
            record = PolicyRecord(
                run_id=event.run_id,
                project_id=event.project_id,
                hook=str(event.hook),
                target=event.target,
                verdict=str(decision.verdict),
                policy=decision.policy or None,
                reason=decision.reason or None,
                detail_json={
                    key: value
                    for key, value in event.data.items()
                    if isinstance(value, str | int | float | bool)
                },
            )
            session.add(record)
            session.flush()
            self.last_record_id = record.id


def registry_view() -> list[dict[str, Any]]:
    """Every policy the owner may turn on."""
    return [
        {
            "id": kind.id,
            "title": kind.title,
            "about": kind.about,
            "hooks": [str(hook) for hook in kind.hooks],
            "parameters": kind.parameters,
        }
        for kind in REGISTRY.values()
    ]
