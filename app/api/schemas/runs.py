"""API request and response shapes: runs."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.schemas.base import StrictRequest


class RunHelp(StrictRequest):
    """The owner's answer to a stuck task: a hint, skipping the step it
    is stuck on, or trying again as it is."""

    action: str = Field(pattern="^(hint|skip|retry)$")
    text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def hint_needs_text(self) -> "RunHelp":
        if self.action == "hint" and not (self.text or "").strip():
            raise ValueError("A hint must say something")
        return self


class RunControlRequest(StrictRequest):
    # "skip" names a plan step in text: it is left out, or stopped where it is.
    kind: str = Field(pattern="^(instruction|pause|cancel|skip|prioritize)$")
    text: str | None = Field(default=None, max_length=4000)
    # Which step the instruction is for. Omitted, the next step to start takes it.
    target_node: str | None = Field(
        default=None, pattern="^(plan|research|strategy|critique|synthesis)$"
    )

    @model_validator(mode="after")
    def instruction_needs_text(self) -> "RunControlRequest":
        if self.kind == "instruction" and not (self.text or "").strip():
            raise ValueError("An instruction must say what to do")
        if self.kind == "skip" and not (self.text or "").strip():
            raise ValueError("Say which step to skip")
        return self


class QueuedMessage(StrictRequest):
    """Words for after the work that is running now."""

    text: str = Field(min_length=1, max_length=4000)


class DirectionEdit(StrictRequest):
    """New words for a note an agent has not taken yet."""

    text: str = Field(min_length=1, max_length=4000)


class IntakeRequest(StrictRequest):
    """A free-text request, as a chat message would arrive."""

    text: str = Field(min_length=1, max_length=4000)
    execute: bool = False


class TaskCreate(StrictRequest):
    project_id: UUID
    type: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=8192)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=32)


class TaskView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    type: str
    objective: str
    status: str
    owner_agent: str | None
    acceptance_criteria: list[str]
    requires_approval: bool


class RunBudget(StrictRequest):
    max_tokens: int = Field(gt=0, le=50_000_000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    max_minutes: int | None = Field(default=None, gt=0, le=1440)


class BudgetRaise(StrictRequest):
    """A new ceiling for a run that reached its old one.

    Every field is optional: the owner raises the limit that was reached and
    leaves the others as they are. A value at or below what is already spent
    would stop the run again at once, so the route refuses it.
    """

    max_tokens: int | None = Field(default=None, gt=0, le=50_000_000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    max_minutes: int | None = Field(default=None, gt=0, le=1440)
    resume: bool = True


class RunCreate(StrictRequest):
    project_id: UUID
    objective: str = Field(min_length=1, max_length=8192)
    # The owner's own short name for the task. Left out or blank, one is
    # derived from the objective.
    title: str | None = Field(default=None, max_length=120)
    budget: RunBudget | None = None
    # The business objective this work is for, when there is one.
    serves_objective_id: UUID | None = None
    # What it should come to: {"shape": ..., "format": ...}. The project's own
    # default applies when this is left out.
    deliverable: dict[str, str] | None = None
    # A task this one builds on, in any project: it is told what that found.
    continues_run_id: UUID | None = None
    # The owner asks for this to be done in the office's browser.
    use_browser: bool = False
    # Files or folders the owner attached for this task.
    attachments: list[UUID] = Field(default_factory=list, max_length=10)
    # The full team pipeline rather than the lead.
    full_team: bool = False


class FollowUp(StrictRequest):
    """The next question, asked of a task that has already finished."""

    objective: str = Field(min_length=1, max_length=8192)
    budget: RunBudget | None = None
    deliverable: dict[str, str] | None = None


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    root_task_id: UUID
    workflow_name: str
    status: str
    current_node: str | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    labels: list[str] = Field(default_factory=list)
    # A short name for this task, derived from its objective in code.
    title: str = ""
    # The task this one continues, when the owner asked a follow-up.
    continues_run_id: UUID | None = None


class LabelsUpdate(StrictRequest):
    """The owner's labels for one task."""

    labels: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("labels")
    @classmethod
    def tidy(cls, value: list[str]) -> list[str]:
        cleaned = []
        for label in value:
            text = " ".join(label.split())[:32]
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned


class RunDetail(RunView):
    # Read from the root task, so it is available before a run finishes and
    # writes its state projection.
    objective: str = ""
    # The latest approval's status, so progress can be shown without the owner
    # token. A resolved approval is a completed step whatever the run does next.
    approval_status: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    project_name: str = ""
    # The business line the task's work is judged by, and who put it there.
    business_line: dict[str, Any] | None = None
    # What the owner asked the task to come to: its shape and format.
    deliverable: dict[str, str] | None = None
    # The full team run the lead proposed here, and the task it became once started.
    team_proposal: dict[str, Any] | None = None


class ArtifactView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    run_id: UUID | None
    type: str
    created_by: str
    version: int
    created_at: datetime


class ApprovalDecisionRequest(StrictRequest):
    """A decision on a specific proposal.

    The digest is required so a decision made against an outdated brief cannot
    authorize newer work. There is deliberately no approver or approved field:
    both are derived server-side.
    """

    scope_digest: str = Field(min_length=64, max_length=64)
    comment: str | None = Field(default=None, max_length=4096)


class RevisionRequest(ApprovalDecisionRequest):
    revision_target: str = Field(pattern="^(research|strategy)$")


class ReworkRequest(StrictRequest):
    """Redo part of a finished task, in the same task."""

    # What should change. Left empty, the office writes the note itself from
    # what the review raised and the answer could not check.
    comment: str = Field(default="", max_length=8192)
    # The part to redo; left out, it is judged from the note, as in review.
    revision_target: str | None = Field(default=None, pattern="^(research|strategy)$")

    @field_validator("comment")
    @classmethod
    def trimmed(cls, value: str) -> str:
        return value.strip()


class HumanReviewRequest(StrictRequest):
    """What a person does with a run that stopped for their attention.

    There is no approver field: identity is derived from the owner token.
    """

    action: str = Field(pattern="^(accept|revise|close)$")
    # Optional in general: accepting or closing needs no instruction. A
    # revision does, because the next attempt has to know what to change.
    comment: str = Field(default="", max_length=8192)
    revision_target: str | None = Field(default=None, pattern="^(research|strategy)$")

    @model_validator(mode="after")
    def revision_needs_an_instruction(self) -> "HumanReviewRequest":
        if self.action == "revise" and not self.comment.strip():
            raise ValueError("A revision must say what should change")
        return self


class ApprovalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    task_id: UUID
    action_type: str
    status: str
    scope_digest: str | None
    resolved_by: str | None
    revision_target: str | None
    requested_at: datetime
    resolved_at: datetime | None


class ArtifactContent(ArtifactView):
    """One artifact version, with the stored document body."""

    task_id: UUID | None = None
    content: str = ""


class PendingApproval(BaseModel):
    approval: ApprovalView
    brief: dict[str, Any]


class RunTitle(StrictRequest):
    """The owner's own name for a task; blank goes back to one made from the ask."""

    title: str | None = Field(default=None, max_length=120)
