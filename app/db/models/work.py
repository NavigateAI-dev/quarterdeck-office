"""Tables for work: ventures, projects, tasks, runs and what they produce."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, CreatedAt, Identity, UpdatedAt


class Venture(Identity, CreatedAt, UpdatedAt, Base):
    """One business line, and how work is judged inside it.

    Projects are work; a venture is the business the work is for. Its doctrine
    is the part that makes the same agents reason differently: what counts as
    evidence here, what is never acceptable, what good work looks like. It
    changes a few times a year, not a few times a week.
    """

    __tablename__ = "ventures"
    __table_args__ = (Index("uq_ventures_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # How work is judged here: the standard of proof and the non-negotiables.
    doctrine: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The same standard, said to one role in particular: what the critic should
    # refuse here, what the researcher must reach for. Keyed by role.
    role_doctrine_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(64), server_default="ACTIVE", index=True)
    # This line's pack: skills offered only to its tasks. A skill in no
    # line's pack is offered to every task.
    skill_ids_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class Objective(Identity, CreatedAt, UpdatedAt, Base):
    """What a business line is trying to achieve, in a stated window.

    A task can say which objective it serves, so finished work means something
    beyond itself. The window is on the objective rather than in a separate
    period, because one person does not need a calendar tier: a period is just
    the objectives that share its dates.
    """

    __tablename__ = "objectives"

    venture_id: Mapped[UUID] = mapped_column(ForeignKey("ventures.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    # What would show it had been achieved. Empty is allowed and honest: an
    # objective without a measure is a direction, and says so.
    measure: Mapped[str] = mapped_column(Text, default="", server_default="")
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    # OPEN while it is being worked towards; the owner closes it as met or
    # missed. Nothing closes it automatically: work is evidence, not a verdict.
    status: Mapped[str] = mapped_column(String(16), server_default="OPEN", index=True)


class Entity(Identity, CreatedAt, UpdatedAt, Base):
    """A recurring subject of the work: a regulator, an instrument, a vendor.

    These are the dots a business connects over time. An office that rediscovers
    what the Wwft is on every task is not learning; one that keeps a short
    dossier about it is.

    Scoped to a business line, because "DNB" matters to compliance work and not
    to marketing. A null venture means the whole office knows it.
    """

    __tablename__ = "entities"
    __table_args__ = (Index("uq_entities_venture_name", "venture_id", "name", unique=True),)

    venture_id: Mapped[UUID | None] = mapped_column(ForeignKey("ventures.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), server_default="subject")
    name: Mapped[str] = mapped_column(String(255))
    # Other ways the work refers to it: "Wwft" is also "BWBR0024282".
    aliases_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="ACTIVE", index=True)


class EntityFact(Identity, CreatedAt, UpdatedAt, Base):
    """One line of an entity's dossier, and what the owner decided about it.

    Proposed from finished work, never written by a model on its own account,
    and told to later tasks only once kept — the same gate memory has, for the
    same reason: a confident falsehood about a regulator is worse than silence.
    """

    __tablename__ = "entity_facts"
    __table_args__ = (Index("uq_entity_facts_digest", "entity_id", "fact_digest", unique=True),)

    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    fact: Mapped[str] = mapped_column(Text)
    fact_digest: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(16), server_default="CANDIDATE", index=True)
    durable_probability: Mapped[float | None] = mapped_column(Float)
    evaluator: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(255))


class Project(Identity, CreatedAt, UpdatedAt, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(64), server_default="ACTIVE", index=True)
    # The business line this work is for. None keeps the office as it was.
    venture_id: Mapped[UUID | None] = mapped_column(ForeignKey("ventures.id"), index=True)
    # Who put it there: "owner" (never moved by the office) or "office".
    venture_set_by: Mapped[str | None] = mapped_column(String(16))
    # The office's last judgment of where it belongs, kept even when unsure.
    venture_suggestion_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # What this project's tasks should come to unless a task says otherwise.
    deliverable_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # A project may run under its own limits, models and channel; anything
    # left unset follows the office.
    monthly_cost_limit_usd: Mapped[float | None] = mapped_column(Float)
    run_max_tokens: Mapped[int | None] = mapped_column(Integer)
    run_max_cost_usd: Mapped[float | None] = mapped_column(Float)
    run_max_minutes: Mapped[int | None] = mapped_column(Integer)
    role_models_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    notify_destination_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channel_destinations.id")
    )
    # Past this much spent, a task finishes on the cheaper model rather than
    # stopping. Both must be set for it to apply.
    downgrade_at_usd: Mapped[float | None] = mapped_column(Float)
    cheap_connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_connections.id"))
    # The advisor on call for this project's tasks; None follows the office.
    advisor_on: Mapped[bool | None] = mapped_column(Boolean)
    # Team mode for this project's tasks; None follows the office.
    team_on: Mapped[bool | None] = mapped_column(Boolean)


class Task(Identity, CreatedAt, UpdatedAt, Base):
    __tablename__ = "tasks"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    objective: Mapped[str] = mapped_column(Text)
    # The title the owner gave it; without one, a title is derived from the
    # objective wherever it is shown (app.titles.title_of).
    title: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, server_default="")
    owner_agent: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), server_default="PENDING", index=True)
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    acceptance_criteria: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    requires_approval: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Run(Identity, Base):
    __tablename__ = "runs"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    root_task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    workflow_name: Mapped[str] = mapped_column(String(128))
    # The business objective this work serves, when the owner named one.
    objective_id: Mapped[UUID | None] = mapped_column(ForeignKey("objectives.id"), index=True)
    # What the owner asked this task to come to: its shape and its format.
    deliverable_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # The task this one continues, when the owner asked a follow-up. Not a
    # foreign key with a cascade: a follow-up is work in its own right and must
    # not disappear with the task that prompted it.
    continues_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    # The topic and the owner's message it was handed off from.
    topic_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    origin_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(64), server_default="PENDING", index=True)
    current_node: Mapped[str | None] = mapped_column(String(128))
    state_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    # The worker executing this run, and how long its hold lasts. Another
    # worker may take the run only once the lease has expired.
    worker_id: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # The owner's own labels for this task, for finding and grouping work.
    labels_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # The agent configuration captured when the run was created. Later edits
    # to agents never change what a run was started with.
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Token/cost/time limits fixed when the run was created.
    budget_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class Artifact(Identity, CreatedAt, Base):
    __tablename__ = "artifacts"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class Approval(Identity, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        # At most one pending approval per run: a second one would let two
        # decisions authorize the same work.
        Index(
            "uq_approvals_run_id_pending",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), server_default="PENDING", index=True)
    # The exact proposal presented, and its digest. An approval authorizes this
    # and nothing else; a changed proposal digests differently.
    scope_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    scope_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    decision_id: Mapped[UUID | None] = mapped_column(ForeignKey("decisions.id"))
    revision_target: Mapped[str | None] = mapped_column(String(32))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)


class EngineeringExecution(Base):
    """One approval carried out. Written before the work starts and
    completed with its report, so recovery never repeats it blindly."""

    __tablename__ = "engineering_executions"

    approval_id: Mapped[UUID] = mapped_column(ForeignKey("approvals.id"), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    scope_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="STARTED")
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Decision(Identity, CreatedAt, Base):
    __tablename__ = "decisions"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(32))
    approved_by: Mapped[str] = mapped_column(String(255))
    revisit_condition: Mapped[str | None] = mapped_column(Text)
    # Said by the owner in a discussion: where, and what became of it.
    topic_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    replaced_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class UsageEvent(Identity, Base):
    """One model invocation's usage, retries included. No prompt or output."""

    __tablename__ = "usage_events"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    project_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model_alias: Mapped[str] = mapped_column(String(64))
    resolved_model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer)
    latency_seconds: Mapped[float] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Why attempts were rejected, in words (never the output).
    error_detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunControl(Identity, CreatedAt, Base):
    """An owner intervention: instruction, pause or cancel, with its lifecycle.

    QUEUED when sent; RECEIVED when a step picks it up (instructions) or the
    run reaches a step boundary (pause, cancel); APPLIED once it took effect.
    """

    __tablename__ = "run_controls"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), server_default="QUEUED")
    # Set when the owner aimed this instruction at one step; it then waits
    # for that step instead of being picked up by the next one to start.
    target_node: Mapped[str | None] = mapped_column(String(32))
    received_node: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelCall(CreatedAt, Base):
    """One model call, as the model got it (`app/model_calls.py`).
    `request_json` names its bodies (messages, schema, tools) by digest."""

    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_run_id_id", "run_id", "id"),
        Index("ix_model_calls_origin_created", "origin", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # A run's call; or one behind a discussion reply (the owner's message it
    # answered) or a Slack thread, with no run.
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"))
    origin: Mapped[str] = mapped_column(String(16), server_default="task")
    topic_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("room_messages.id", ondelete="CASCADE"), index=True
    )
    chat_ref: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(32))
    model_alias: Mapped[str] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(255))
    request_digest: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    # ok, or the gateway's error code
    status: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_seconds: Mapped[float | None] = mapped_column(Float)


class ModelPayload(CreatedAt, Base):
    """A body a kept model call names: a message, a schema, a tool list, an answer.
    Kept once, by the digest of its content."""

    __tablename__ = "model_payloads"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[Any] = mapped_column(JSONB)
    size: Mapped[int] = mapped_column(Integer)


class RunStateChange(CreatedAt, Base):
    """One change to a run's state (`app/run_state.py`): set some keys, or
    replace the whole. `runs.state_json` is what they add up to."""

    __tablename__ = "run_state_changes"
    __table_args__ = (Index("ix_run_state_changes_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"))
    # set | replace
    op: Mapped[str] = mapped_column(String(8))
    patch_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Who changed it: start, dispatch, lead, context, finish, review, baseline.
    source: Mapped[str] = mapped_column(String(32))


class SideEffect(CreatedAt, Base):
    """A write that leaves the office, written down before it is sent (Stage 5c,
    `app/side_effects.py`): intended, then done, failed or unknown. The call's
    arguments are not kept, only a digest of them in `key`."""

    __tablename__ = "side_effects"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_side_effects_run_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    # What it was, as the owner reads it: "create_issue on GitHub".
    subject: Mapped[str] = mapped_column(String(200))
    # intended | done | failed | unknown
    status: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(64))
    settled_by: Mapped[str | None] = mapped_column(String(128))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(CreatedAt, Base):
    """A durable progress event. The id is the monotonic cursor clients resume from."""

    __tablename__ = "run_events"
    # A run's events in order: the feed and the live stream read them by it (0069).
    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class TaskTemplate(Identity, CreatedAt, UpdatedAt, Base):
    """A reusable objective with its project and budget. Outlives its project."""

    __tablename__ = "task_templates"
    __table_args__ = (Index("uq_task_templates_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255))
    # Cleared when the project is deleted; its schedules are then disabled.
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    objective: Mapped[str] = mapped_column(Text)
    budget_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    source_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class Schedule(Identity, CreatedAt, UpdatedAt, Base):
    """Runs a template at a local time, or is a routine (app/routines) with its own
    title and prompt; see app/scheduling.py for the rules."""

    __tablename__ = "schedules"

    template_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_templates.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    # A routine's words. None on a schedule that runs a template.
    prompt: Mapped[str | None] = mapped_column(Text)
    # When a routine should send nothing, in the owner's words.
    quiet_when: Mapped[str] = mapped_column(Text, server_default="")
    every_hours: Mapped[int | None] = mapped_column(Integer)
    cron: Mapped[str | None] = mapped_column(String(64))
    once_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preset: Mapped[str | None] = mapped_column(String(32))
    agent_profile_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(String(64))
    frequency: Mapped[str] = mapped_column(String(16))
    local_time: Mapped[str] = mapped_column(String(5))
    weekday: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_outcome: Mapped[str | None] = mapped_column(String(64))
    disabled_reason: Mapped[str | None] = mapped_column(String(64))
    # A chat channel each run's result is posted to, answer and file.
    post_to_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channel_destinations.id", ondelete="SET NULL")
    )
    # The discussion a routine answers in.
    topic_id: Mapped[UUID | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))


class ProjectMemory(Identity, CreatedAt, UpdatedAt, Base):
    """A lesson a finished run produced, and what the owner decided about it.

    Project-scoped on purpose: what one project learned about its sources is
    not automatically true for another. Nothing here reaches an agent until the
    owner approves it.
    """

    __tablename__ = "project_memory"
    __table_args__ = (
        Index("uq_project_memory_lesson", "project_id", "lesson_digest", unique=True),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    # Where it came from, so an approved lesson can always be traced back.
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    lesson: Mapped[str] = mapped_column(Text)
    lesson_digest: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(16), server_default="CANDIDATE", index=True)
    # Where an approved lesson applies: this project, its whole business line,
    # or everywhere. Chosen by the owner when they keep it.
    scope: Mapped[str] = mapped_column(String(16), server_default="project")
    # Recorded when a lesson is kept for a venture, so recall needs no join.
    venture_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    # What the evaluator thought, with which model, when. Null when never asked.
    reusable_probability: Mapped[float | None] = mapped_column(Float)
    evaluator: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(255))


class LimitAlert(Identity, Base):
    """A warning at 80% or 100% of a limit, recorded once per crossing."""

    __tablename__ = "limit_alerts"
    __table_args__ = (
        Index("uq_limit_alerts_crossing", "scope", "subject", "window", "threshold", unique=True),
    )

    scope: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    window: Mapped[str] = mapped_column(String(16))
    threshold: Mapped[int] = mapped_column(Integer)
    share: Mapped[float] = mapped_column(Float)
    # When it was queued for chat; a warning is announced once.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TeamPost(Identity, Base):
    """One post in a task's room (T): what an agent found, doubts, asks or offers."""

    __tablename__ = "team_posts"
    __table_args__ = (
        UniqueConstraint("run_id", "key", name="uq_team_posts_run_key"),
        Index("ix_team_posts_run_id", "run_id", "created_at"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    author_role: Mapped[str] = mapped_column(String(32))
    step_id: Mapped[str | None] = mapped_column(String(64))
    to_role: Mapped[str | None] = mapped_column(String(32))
    to_step: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    wave: Mapped[int | None] = mapped_column(Integer)
    # The stage it was said in (plan, work, huddle, review, answer), and the key
    # of the post it answers (0042).
    phase: Mapped[str | None] = mapped_column(String(16))
    reply_to: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
