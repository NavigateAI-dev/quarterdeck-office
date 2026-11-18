import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import run_state
from app.agent_profiles import capture_agent_snapshot
from app.artifacts import ArtifactMetadata, ArtifactStore
from app.db.models import (
    Artifact,
    Project,
    Run,
    RunControl,
    RunEvent,
    Task,
    TeamPost,
    UsageEvent,
)
from app.events import InvocationEvent
from app.orchestrator.graph import ANALYSIS_WORKFLOW, RUN_WORKFLOWS
from app.orchestrator.state import AnalysisState, RunStatus
from app.run_asks import BUDGET_ASK, STUCK_ASK, pending_ask
from app.runtimes.steering import KINDS

WORKFLOW = ANALYSIS_WORKFLOW
# How long a worker's hold on a run lasts without being renewed. Long enough
# for one step, short enough that a dead worker frees its work quickly.
LEASE_SECONDS = 180
# What a run was told and who it was handed to: kept when its final state
# replaces the record's and does not carry them (5d).
KEPT_AT_FINISH = ("context", "handed_to", "reviewed_by")
# A message the owner queued for after the work, and the kinds of the
# owner's words a waiting note may be reworded or taken back as.
QUEUED_MESSAGE = "queued"
WORDS = ("instruction", QUEUED_MESSAGE)

# One artifact per role output per run, so a retry reuses the same identity.
ARTIFACT_OF_STAGE = {
    "plan": ("task_plan", "planner"),
    "research": ("research_report", "researcher"),
    "strategy": ("strategy_proposal", "strategist"),
    "critique": ("critique", "critic"),
    "synthesis": ("synthesis", "chief"),
    "engineering": ("engineering_report", "engineer"),
    # Assembled by the application from recorded evidence, not by a model.
    "final_report": ("final_report", "application"),
    "human_review": ("human_review", "owner"),
}


class UnknownProject(Exception):
    """The requested project does not exist."""


class InstructionTaken(Exception):
    """An agent already has this note, so it cannot be changed or withdrawn."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


@dataclass(frozen=True)
class Dispatch:
    run_id: UUID
    task_id: UUID
    project_id: UUID


def artifact_id_for(run_id: UUID, stage: str, version: int) -> UUID:
    """Derive a stable artifact identity.

    A repeated dispatch of the same run and stage resolves to the same ID, so a
    retry cannot create a second artifact for work already recorded.
    """
    return uuid5(NAMESPACE_URL, f"run:{run_id}/stage:{stage}/v{version}")


class RunRepository:
    """Durable run bookkeeping. The graph checkpoint remains authoritative for
    continuation; these rows are the domain projection of it."""

    def __init__(self, session_factory: sessionmaker[Session], store: ArtifactStore) -> None:
        self.session_factory = session_factory
        self.store = store

    def record_usage(self, event: InvocationEvent) -> None:
        if event.run_id is None:
            return
        with self.session_factory() as session, session.begin():
            session.add(
                UsageEvent(
                    run_id=event.run_id,
                    task_id=event.task_id,
                    project_id=event.project_id,
                    role=event.role,
                    provider=event.provider,
                    model_alias=event.model_alias,
                    resolved_model=event.resolved_model,
                    status=event.status,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    estimated_cost=event.estimated_cost,
                    attempts=event.attempts,
                    latency_seconds=event.latency_seconds,
                    error_code=event.error_code,
                    error_detail=event.error_detail,
                    occurred_at=event.occurred_at,
                )
            )

    def budget(self, run_id: UUID) -> dict[str, Any]:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            return dict(run.budget_json or {}) if run is not None else {}

    def set_budget(self, run_id: UUID, budget: dict[str, Any]) -> None:
        with self.session_factory() as session, session.begin():
            session.execute(update(Run).where(Run.id == run_id).values(budget_json=budget))

    def spent_tokens(self, run_id: UUID) -> int:
        with self.session_factory() as session:
            total = session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            func.coalesce(UsageEvent.prompt_tokens, 0)
                            + func.coalesce(UsageEvent.completion_tokens, 0)
                        ),
                        0,
                    )
                ).where(UsageEvent.run_id == run_id)
            )
            return int(total or 0)

    def usage(self, run_id: UUID) -> dict[str, Any]:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            events = list(session.scalars(select(UsageEvent).where(UsageEvent.run_id == run_id)))
        by_role: dict[str, dict[str, Any]] = {}
        for event in events:
            row = by_role.setdefault(
                event.role, {"role": event.role, "calls": 0, "tokens": 0, "cost_usd": 0.0}
            )
            row["calls"] += 1
            row["tokens"] += (event.prompt_tokens or 0) + (event.completion_tokens or 0)
            row["cost_usd"] += event.estimated_cost or 0.0
        costs = [event.estimated_cost for event in events]
        return {
            "budget": (run.budget_json if run else {}) or {},
            "spent": {
                "tokens": sum(row["tokens"] for row in by_role.values()),
                "cost_usd": sum(c for c in costs if c is not None)
                if any(c is not None for c in costs)
                else None,
                "cost_reported": all(c is not None for c in costs),
                "calls": len(events),
            },
            "by_role": sorted(by_role.values(), key=lambda row: str(row["role"])),
        }

    def create(
        self,
        project_id: UUID,
        objective: str,
        budget: dict[str, Any] | None = None,
        workflow: str = WORKFLOW,
        origin: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> Dispatch:
        """Record a run. `origin` names the chat thread (and agent) that asked for
        it; `title` is the owner's own name for the task, when they gave one."""
        if workflow not in RUN_WORKFLOWS:
            raise ValueError(f"unknown workflow: {workflow}")
        with self.session_factory() as session, session.begin():
            if session.get(Project, project_id) is None:
                raise UnknownProject(str(project_id))
            task = Task(
                project_id=project_id,
                type="analysis",
                objective=objective,
                title=" ".join((title or "").split()) or None,
                owner_agent="chief",
                requires_approval=True,
            )
            session.add(task)
            session.flush()
            run = Run(
                project_id=project_id,
                root_task_id=task.id,
                workflow_name=workflow,
                status="PENDING",
                # Captured in the same transaction as the run, so a run never
                # exists without the configuration it was started with.
                config_snapshot_json={
                    **capture_agent_snapshot(session),
                    **({"origin": origin} if origin else {}),
                },
                budget_json=budget or {},
            )
            session.add(run)
            session.flush()
            return Dispatch(run_id=run.id, task_id=task.id, project_id=project_id)

    def workflow(self, run_id: UUID) -> str:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            return run.workflow_name if run is not None else WORKFLOW

    def claim(self, run_id: UUID, worker_id: str = "", lease_seconds: int = LEASE_SECONDS) -> bool:
        """Take exclusive ownership of a run, for a bounded time.

        The status transition is the claim. A second caller sees no eligible row
        and must not execute, so duplicate dispatch cannot run a run twice. The
        lease bounds how long the claim survives a worker that stops without
        saying so: only an expired lease may be taken over.
        """
        with self.session_factory() as session, session.begin():
            result = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status.in_(("PENDING", "RECOVERING")))
                .values(
                    status=RunStatus.RUNNING,
                    started_at=func.now(),
                    worker_id=worker_id or None,
                    lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                )
                .returning(Run.id)
            )
            return result.scalar_one_or_none() is not None

    def renew_lease(self, run_id: UUID, worker_id: str, lease_seconds: int = LEASE_SECONDS) -> bool:
        """Extend this worker's hold. False once another worker has taken it."""
        with self.session_factory() as session, session.begin():
            result = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == RunStatus.RUNNING,
                    (Run.worker_id == worker_id) | (Run.worker_id.is_(None)),
                )
                .values(
                    worker_id=worker_id,
                    lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                )
                .returning(Run.id)
            )
            return result.scalar_one_or_none() is not None

    def release_lease(self, run_id: UUID) -> None:
        """Give the run up: it is no longer being executed here."""
        with self.session_factory() as session, session.begin():
            session.execute(
                update(Run).where(Run.id == run_id).values(worker_id=None, lease_expires_at=None)
            )

    def requeue(self, run_id: UUID) -> bool:
        """Return a waiting run to the queue so the worker can claim it.

        A run is claimed by transitioning it out of PENDING, so every path that
        resumes a run must put it back there first. Resuming without this is
        silently dropped.
        """
        with self.session_factory() as session, session.begin():
            result = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status.in_(("WAITING_FOR_APPROVAL", "PENDING", "PAUSED")),
                )
                .values(status="PENDING")
                .returning(Run.id)
            )
            return result.scalar_one_or_none() is not None

    def active_run_ids(self) -> list[UUID]:
        with self.session_factory() as session:
            rows = session.execute(
                select(Run.id).where(Run.status == RunStatus.RUNNING).order_by(Run.id)
            )
            return [row[0] for row in rows]

    def mark_for_recovery(self) -> list[UUID]:
        """Reclaim runs an interrupted process left behind.

        PENDING is included deliberately: the work queue is in memory, so a run
        whose row exists but whose job was never consumed would otherwise sit
        queued forever with nothing to pick it up.
        """
        with self.session_factory() as session, session.begin():
            rows = session.execute(
                update(Run)
                .where(
                    Run.status.in_((RunStatus.RUNNING, "PENDING")),
                    # A run a live worker still holds is not ours to reclaim;
                    # one with no holder, or whose hold has run out, is.
                    (Run.worker_id.is_(None))
                    | (Run.lease_expires_at.is_(None))
                    | (Run.lease_expires_at < func.now()),
                )
                .values(status="RECOVERING", worker_id=None, lease_expires_at=None)
                .returning(Run.id, Run.current_node)
            )
            reclaimed = [(row[0], row[1]) for row in rows]
        for run_id, node in reclaimed:
            # Said out loud: the owner watched tasks flip to "Resuming" with no
            # account of what had happened to them.
            self.add_event(run_id, "run_recovered", {"from_node": node or ""})
        return [run_id for run_id, _ in reclaimed]

    def has_started(self, run_id: UUID) -> bool:
        """Whether a run ever began, and so has a checkpoint to resume from."""
        with self.session_factory() as session:
            started = session.execute(
                select(Run.started_at).where(Run.id == run_id)
            ).scalar_one_or_none()
            return started is not None

    def record_stage(
        self,
        dispatch: Dispatch,
        stage: str,
        payload: dict[str, Any],
        created_by: str | None = None,
    ) -> UUID | None:
        """Persist one role output as an immutable artifact version.

        Writing the file and inserting the row are both keyed by the derived
        artifact ID, so replay after a crash reconciles instead of duplicating.
        `created_by` names another author, such as the owner editing an answer.
        """
        artifact_type, stage_author = ARTIFACT_OF_STAGE[stage]
        created_by = created_by or stage_author
        version = self._next_version(dispatch, artifact_type)
        artifact_id = artifact_id_for(dispatch.run_id, stage, version)
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            project_id=dispatch.project_id,
            task_id=dispatch.task_id,
            run_id=dispatch.run_id,
            type=artifact_type,
            created_by=created_by,
            version=version,
        )
        # Artifacts are documents a person reads, so they keep their own script
        # rather than being escaped into ASCII.
        body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        try:
            path = self.store.write(metadata, body)
        except FileExistsError:
            path = self.store.path_of(metadata)
        with self.session_factory() as session, session.begin():
            statement = (
                insert(Artifact)
                .values(
                    id=artifact_id,
                    project_id=dispatch.project_id,
                    task_id=dispatch.task_id,
                    run_id=dispatch.run_id,
                    type=artifact_type,
                    path=str(path),
                    created_by=created_by,
                    version=version,
                    metadata_json=ArtifactStore.metadata_json(metadata),
                )
                .on_conflict_do_nothing(index_elements=[Artifact.id])
                .returning(Artifact.id)
            )
            return session.execute(statement).scalar_one_or_none()

    def record_subtask(
        self, dispatch: Dispatch, index: int, question: str, payload: dict[str, Any]
    ) -> UUID | None:
        """Persist one parallel subtask as a child task with its own artifact.

        The child task's identity derives from the run and subtask position, so
        a replayed step reuses it rather than creating another.
        """
        child_id = uuid5(NAMESPACE_URL, f"run:{dispatch.run_id}/subtask:{index}")
        with self.session_factory() as session, session.begin():
            session.execute(
                insert(Task)
                .values(
                    id=child_id,
                    project_id=dispatch.project_id,
                    parent_task_id=dispatch.task_id,
                    type="research_subtask",
                    objective=question,
                    owner_agent="researcher",
                    status="COMPLETED",
                    requires_approval=False,
                )
                .on_conflict_do_update(
                    index_elements=[Task.id], set_={"objective": question, "status": "COMPLETED"}
                )
            )
            version = (
                session.scalar(
                    select(func.count()).where(
                        Artifact.task_id == child_id, Artifact.type == "research_subtask"
                    )
                )
                or 0
            ) + 1
        artifact_id = uuid5(NAMESPACE_URL, f"run:{dispatch.run_id}/subtask:{index}/v{version}")
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            project_id=dispatch.project_id,
            task_id=child_id,
            run_id=dispatch.run_id,
            type="research_subtask",
            created_by="researcher",
            version=version,
        )
        body = json.dumps({"question": question, "report": payload}, indent=2, ensure_ascii=False)
        try:
            path = self.store.write(metadata, body)
        except FileExistsError:
            path = self.store.path_of(metadata)
        with self.session_factory() as session, session.begin():
            return session.execute(
                insert(Artifact)
                .values(
                    id=artifact_id,
                    project_id=dispatch.project_id,
                    task_id=child_id,
                    run_id=dispatch.run_id,
                    type="research_subtask",
                    path=str(path),
                    created_by="researcher",
                    version=version,
                    metadata_json=ArtifactStore.metadata_json(metadata),
                )
                .on_conflict_do_nothing(index_elements=[Artifact.id])
                .returning(Artifact.id)
            ).scalar_one_or_none()

    # --- Owner controls and progress events (slice 4) -------------------------

    def add_event(self, run_id: UUID, kind: str, payload: dict[str, Any] | None = None) -> int:
        with self.session_factory() as session, session.begin():
            event = RunEvent(run_id=run_id, kind=kind, payload_json=payload or {})
            session.add(event)
            session.flush()
            return int(event.id)

    def pending_budget_ask(self, run_id: UUID) -> dict[str, Any] | None:
        """The ceiling question this run is waiting on, if it has one."""
        with self.session_factory() as session:
            return pending_ask(session, run_id, BUDGET_ASK)

    def pending_stuck_ask(self, run_id: UUID) -> dict[str, Any] | None:
        """What a stuck step asks the owner, until the task goes on or ends."""
        with self.session_factory() as session:
            return pending_ask(session, run_id, STUCK_ASK)

    def binding_ceiling(self, run_id: UUID, smallest_call_tokens: int) -> str | None:
        """The ceiling that would stop this run again the moment it resumed.

        A time ceiling is not one of them: minutes are counted per worker job,
        so a resumed run starts that clock again. Tokens and money accumulate
        across resumes, so without a raise the run stops on its first call and
        nothing the owner can see has changed.
        """
        ask = self.pending_budget_ask(run_id)
        if ask is None:
            return None
        budget = self.budget(run_id)
        spent = self.usage(run_id)["spent"]
        max_tokens = budget.get("max_tokens")
        if max_tokens is not None:
            headroom = int(max_tokens) - int(spent.get("tokens") or 0)
            if headroom < smallest_call_tokens:
                return "tokens"
        max_cost = budget.get("max_cost_usd")
        cost = spent.get("cost_usd")
        if max_cost is not None and cost is not None and float(cost) >= float(max_cost):
            return "cost"
        return None

    def recent_events(self, run_id: UUID, kind: str, limit: int = 12) -> list[dict[str, Any]]:
        """The latest events of one kind, newest first."""
        with self.session_factory() as session:
            rows = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.kind == kind)
                .order_by(RunEvent.id.desc())
                .limit(limit)
            )
            return [dict(row.payload_json or {}) for row in rows]

    def events(self, run_id: UUID, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.id > after)
                .order_by(RunEvent.id)
                .limit(limit)
            )
            return [
                {
                    "cursor": row.id,
                    "kind": row.kind,
                    "payload": row.payload_json or {},
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    @staticmethod
    def _control_view(row: RunControl) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "kind": row.kind,
            "text": row.text,
            "status": row.status,
            "target_node": row.target_node,
            "received_node": row.received_node,
            "received_at": row.received_at,
            "applied_at": row.applied_at,
            "created_at": row.created_at,
        }

    def add_control(
        self,
        run_id: UUID,
        kind: str,
        text: str | None = None,
        target_node: str | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as session, session.begin():
            row = RunControl(
                run_id=run_id, kind=kind, text=text, status="QUEUED", target_node=target_node
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            view = self._control_view(row)
        self.add_event(
            run_id, "control_queued", {"control_id": view["id"], "kind": kind, "text": text}
        )
        return view

    def controls(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(RunControl)
                .where(RunControl.run_id == run_id)
                .order_by(RunControl.created_at)
            )
            return [self._control_view(row) for row in rows]

    def change_instruction(
        self, run_id: UUID, control_id: UUID, text: str
    ) -> dict[str, Any] | None:
        """Reword a note the agents have not taken yet."""
        with self.session_factory() as session, session.begin():
            row = session.get(RunControl, control_id)
            if row is None or row.run_id != run_id or row.kind not in WORDS:
                return None
            if row.status != "QUEUED":
                raise InstructionTaken(row.status)
            row.text = text
            session.flush()
            session.refresh(row)
            return self._control_view(row)

    def withdraw_instruction(self, run_id: UUID, control_id: UUID) -> bool:
        """Take back a note the agents have not taken yet."""
        with self.session_factory() as session, session.begin():
            row = session.get(RunControl, control_id)
            if row is None or row.run_id != run_id or row.kind not in WORDS:
                return False
            if row.status != "QUEUED":
                raise InstructionTaken(row.status)
            session.delete(row)
            return True

    def steer_queued(self, run_id: UUID, control_id: UUID) -> dict[str, Any] | None:
        """A message queued for after the work, sent now instead: it
        becomes an instruction the next step takes."""
        with self.session_factory() as session, session.begin():
            row = session.get(RunControl, control_id)
            if row is None or row.run_id != run_id or row.kind != QUEUED_MESSAGE:
                return None
            if row.status != "QUEUED":
                raise InstructionTaken(row.status)
            row.kind = "instruction"
            session.flush()
            return self._control_view(row)

    def take_queued(self, run_id: UUID) -> list[str]:
        """The messages queued for after the work, taken once, oldest first."""
        now = datetime.now(UTC)
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(RunControl)
                    .where(
                        RunControl.run_id == run_id,
                        RunControl.kind == QUEUED_MESSAGE,
                        RunControl.status == "QUEUED",
                    )
                    .order_by(RunControl.created_at)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.status = "APPLIED"
                row.applied_at = now
            return [str(row.text) for row in rows if row.text]

    def receive_instructions(self, run_id: UUID, node: str) -> list[str]:
        """Mark queued instructions received by this step; return every instruction.

        Instructions persist for the rest of the run: guidance such as "focus on
        Europe" applies to every later step, not only the next one.
        """
        now = datetime.now(UTC)
        # An instruction aimed at another step is not this step's business: it
        # waits, so the wrong role is never told to act on it.
        for_this_step = RunControl.target_node.is_(None) | (RunControl.target_node == node)
        with self.session_factory() as session, session.begin():
            session.execute(
                update(RunControl)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind == "instruction",
                    RunControl.status == "QUEUED",
                    for_this_step,
                )
                .values(status="RECEIVED", received_node=node, received_at=now)
            )
            rows = session.scalars(
                select(RunControl.text)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind == "instruction",
                    RunControl.status != "QUEUED",
                    for_this_step,
                )
                .order_by(RunControl.created_at)
            )
            texts = [str(text) for text in rows if text]
        if texts:
            self.add_event(run_id, "instructions_received", {"node": node, "count": len(texts)})
        return texts

    def pending_build_notes(self, run_id: UUID) -> list[dict[str, Any]]:
        """The owner's comments to a build not yet given to its agent."""

        with self.session_factory() as session:
            rows = session.scalars(
                select(RunControl)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind.in_(KINDS),
                    RunControl.status == "QUEUED",
                )
                .order_by(RunControl.created_at)
            )
            return [{"id": str(row.id), "kind": row.kind, "text": row.text} for row in rows]

    def take_build_notes(self, run_id: UUID, ids: list[str]) -> None:
        """Mark comments given to the agent, so each is delivered once."""
        if not ids:
            return
        now = datetime.now(UTC)
        with self.session_factory() as session, session.begin():
            for row in session.scalars(
                select(RunControl).where(
                    RunControl.run_id == run_id,
                    RunControl.id.in_([UUID(given) for given in ids]),
                    RunControl.status == "QUEUED",
                )
            ):
                row.status = "APPLIED"
                row.received_node = "engineering"
                row.received_at = now
                row.applied_at = now

    def take_new_instructions(self, run_id: UUID, node: str) -> list[str]:
        """Notes the owner sent since the step began, taken now, mid-step.

        Only the ones not yet received: what the step was given when it began
        is not repeated. Each one taken is announced, so the owner sees it
        arrived.
        """
        now = datetime.now(UTC)
        for_this_step = RunControl.target_node.is_(None) | (RunControl.target_node == node)
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(RunControl)
                    .where(
                        RunControl.run_id == run_id,
                        RunControl.kind == "instruction",
                        RunControl.status == "QUEUED",
                        for_this_step,
                    )
                    .order_by(RunControl.created_at)
                    .with_for_update(skip_locked=True)
                )
            )
            texts = [str(row.text) for row in rows if row.text]
            for row in rows:
                row.status = "RECEIVED"
                row.received_node = node
                row.received_at = now
        if texts:
            self.add_event(
                run_id,
                "instructions_received",
                {"node": node, "count": len(texts), "mid_step": True},
            )
        return texts

    def prioritized_steps(self, run_id: UUID) -> list[str]:
        """The plan steps the owner asked to be done next, the latest first (13d)."""
        with self.session_factory() as session:
            rows = session.scalars(
                select(RunControl.text)
                .where(RunControl.run_id == run_id, RunControl.kind == "prioritize")
                .order_by(RunControl.created_at.desc(), RunControl.id.desc())
            )
            return list(dict.fromkeys(str(text) for text in rows if text))

    def skipped_steps(self, run_id: UUID) -> set[str]:
        """The plan steps the owner chose to skip."""
        with self.session_factory() as session:
            return {
                str(text)
                for text in session.scalars(
                    select(RunControl.text).where(
                        RunControl.run_id == run_id, RunControl.kind == "skip"
                    )
                )
                if text
            }

    def apply_instructions(self, run_id: UUID, node: str) -> None:
        with self.session_factory() as session, session.begin():
            session.execute(
                update(RunControl)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind == "instruction",
                    RunControl.status == "RECEIVED",
                    RunControl.received_node == node,
                )
                .values(status="APPLIED", applied_at=datetime.now(UTC))
            )

    def pending_stop(self, run_id: UUID) -> dict[str, Any] | None:
        """The pause or cancel waiting for a step boundary; cancel wins."""
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(RunControl).where(
                        RunControl.run_id == run_id,
                        RunControl.kind.in_(("pause", "cancel")),
                        RunControl.status == "QUEUED",
                    )
                )
            )
        if not rows:
            return None
        chosen = next((row for row in rows if row.kind == "cancel"), rows[0])
        return self._control_view(chosen)

    def apply_stop_controls(self, run_id: UUID) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session, session.begin():
            session.execute(
                update(RunControl)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind.in_(("pause", "cancel")),
                    RunControl.status == "QUEUED",
                )
                .values(status="APPLIED", received_at=now, applied_at=now)
            )

    def expire_stop_controls(self, run_id: UUID) -> None:
        """Pause/cancel requests that arrived as the run ended had nothing to stop."""
        with self.session_factory() as session, session.begin():
            session.execute(
                update(RunControl)
                .where(
                    RunControl.run_id == run_id,
                    RunControl.kind.in_(("pause", "cancel")),
                    RunControl.status == "QUEUED",
                )
                .values(status="EXPIRED")
            )

    def stop(
        self,
        run_id: UUID,
        status: str,
        only_from: tuple[str, ...] | None = None,
        reason: str | None = None,
    ) -> bool:
        """Move a run to PAUSED or CANCELLED. With only_from, only from those statuses.
        A reason says why when it was not the owner's own doing."""
        values: dict[str, Any] = {"status": status}
        if status == RunStatus.CANCELLED:
            values["completed_at"] = func.now()
        statement = update(Run).where(Run.id == run_id)
        if only_from is not None:
            statement = statement.where(Run.status.in_(only_from))
        with self.session_factory() as session, session.begin():
            changed = session.execute(statement.values(**values).returning(Run.id)).first()
        if changed is not None:
            self.add_event(
                run_id,
                "run_paused" if status == RunStatus.PAUSED else "run_cancelled",
                {"reason": reason} if reason else None,
            )
        return changed is not None

    def _next_version(self, dispatch: Dispatch, artifact_type: str) -> int:
        with self.session_factory() as session:
            existing = session.execute(
                select(Artifact.version)
                .where(
                    Artifact.run_id == dispatch.run_id,
                    Artifact.type == artifact_type,
                )
                .order_by(Artifact.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            return (existing or 0) + 1

    def describe(self, run_ids: list[UUID]) -> list[tuple[Dispatch, str, dict[str, Any]]]:
        """Rebuild dispatch details for runs being recovered."""
        described: list[tuple[Dispatch, str, dict[str, Any]]] = []
        with self.session_factory() as session:
            rows = session.execute(
                select(Run, Task).join(Task, Run.root_task_id == Task.id).where(Run.id.in_(run_ids))
            ).all()
            for run, task in rows:
                dispatch = Dispatch(run_id=run.id, task_id=task.id, project_id=run.project_id)
                context = run.state_json.get("context", {}) if run.state_json else {}
                described.append((dispatch, task.objective, context))
        return described

    def add_team_post(self, run_id: UUID, entry: dict[str, Any]) -> None:
        """One post to the task's room; the same key twice is kept once (T)."""
        with self.session_factory() as session, session.begin():
            session.execute(
                insert(TeamPost)
                .values(
                    run_id=run_id,
                    key=str(entry["key"])[:64],
                    kind=str(entry["kind"])[:16],
                    author_role=str(entry["author_role"])[:32],
                    step_id=(str(entry["step_id"])[:64] if entry.get("step_id") else None),
                    to_role=(str(entry["to_role"])[:32] if entry.get("to_role") else None),
                    to_step=(str(entry["to_step"])[:64] if entry.get("to_step") else None),
                    text=str(entry["text"]),
                    wave=entry.get("wave"),
                    phase=(str(entry["phase"])[:16] if entry.get("phase") else None),
                    reply_to=(str(entry["reply_to"])[:64] if entry.get("reply_to") else None),
                )
                .on_conflict_do_nothing(index_elements=["run_id", "key"])
            )

    def team_posts(self, run_id: UUID, limit: int = 200) -> list[dict[str, Any]]:
        """The task's room, oldest first."""
        with self.session_factory() as session:
            rows = session.scalars(
                select(TeamPost)
                .where(TeamPost.run_id == run_id)
                .order_by(TeamPost.created_at, TeamPost.id)
                .limit(limit)
            )
            return [team_post_view(row) for row in rows]

    def live_state(self, run_id: UUID) -> dict[str, Any]:
        """What a running task's record holds of its state now."""
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            return dict(run.state_json or {}) if run is not None else {}

    def merge_state(self, run_id: UUID, values: dict[str, Any]) -> None:
        """Write parts of a running task's state to its record, keeping the rest."""
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                return
            run_state.write(session, run, run_state.SET, values, "dispatch")

    def set_context(self, run_id: UUID, context: dict[str, Any]) -> None:
        """Replace the context stored with a run, keeping the rest of its state."""
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                return
            run_state.write(session, run, run_state.SET, {"context": context}, "context")

    def continued_run(self, run_id: UUID) -> UUID | None:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            return run.continues_run_id if run is not None else None

    def snapshot(self, run_id: UUID) -> dict[str, Any]:
        """The agent configuration captured when the run was created."""
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            return dict(run.config_snapshot_json or {}) if run is not None else {}

    def note_progress(self, run_id: UUID, node: str) -> None:
        """Record the last completed node without terminating the run."""
        with self.session_factory() as session, session.begin():
            session.execute(update(Run).where(Run.id == run_id).values(current_node=node))

    def finish(self, run_id: UUID, state: AnalysisState, current_node: str | None = None) -> None:
        """Project the terminal graph state onto the run row.

        The terminal state replaces the record's, keeping what the run was told
        and who it was handed to when the final state does not carry them: a
        lead run's own state has no `context`, and it was lost here (5d)."""
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                return
            final = dict(state)
            held = dict(run.state_json or {})
            for key in KEPT_AT_FINISH:
                if key not in final and key in held:
                    final[key] = held[key]
            run_state.write(session, run, run_state.REPLACE, final, "finish")
            run.status = state.get("status", RunStatus.COMPLETED)
            run.current_node = current_node
            run.completed_at = func.now()
            run.error = state.get("stopped_reason")

    def fail(
        self,
        run_id: UUID,
        node: str | None,
        code: str,
        only_from: tuple[str, ...] | None = None,
    ) -> bool:
        """Mark a run failed. With only_from, only from those statuses; with no
        node, the step it stopped at is kept. Returns whether it changed."""
        values: dict[str, Any] = {"status": RunStatus.FAILED, "completed_at": func.now()}
        if node:
            values["current_node"] = node
            values["error"] = f"{node}:{code}"
        else:
            values["error"] = code
        statement = update(Run).where(Run.id == run_id)
        if only_from is not None:
            statement = statement.where(Run.status.in_(only_from))
        with self.session_factory() as session, session.begin():
            changed = session.execute(statement.values(**values).returning(Run.id)).first()
        return changed is not None


def team_post_view(row: Any) -> dict[str, Any]:
    return {
        "key": row.key,
        "kind": row.kind,
        "author_role": row.author_role,
        "step_id": row.step_id,
        "to_role": row.to_role,
        "to_step": row.to_step,
        "text": row.text,
        "wave": row.wave,
        "phase": row.phase,
        "reply_to": row.reply_to,
        "at": row.created_at.isoformat() if row.created_at else None,
    }
