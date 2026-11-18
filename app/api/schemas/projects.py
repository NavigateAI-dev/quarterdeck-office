"""API request and response shapes: projects."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.schemas.base import ROLE_DOCTRINE, StrictRequest
from app.api.schemas.runs import RunBudget


class ProjectDelete(StrictRequest):
    """Typed confirmation: the project's exact name."""

    confirm: str = Field(min_length=1, max_length=255)


class ProjectSettingsUpdate(StrictRequest):
    """A project's own limits, models and channel. Null returns one to the office's."""

    monthly_cost_limit_usd: float | None = Field(default=None, gt=0, le=100_000)
    run_max_tokens: int | None = Field(default=None, gt=0, le=50_000_000)
    run_max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    run_max_minutes: int | None = Field(default=None, gt=0, le=1440)
    role_models: dict[str, UUID | None] | None = Field(default=None, max_length=12)
    notify_destination_id: UUID | None = None
    # Past this much spent, finish on the cheaper model instead of stopping.
    downgrade_at_usd: float | None = Field(default=None, gt=0, le=10_000)
    cheap_connection_id: UUID | None = None
    # The advisor on call for this project; null follows the office.
    advisor_on: bool | None = None
    # Team mode for this project; null follows the office.
    team_on: bool | None = None


class ProjectCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=8192)


class ProjectUpdate(StrictRequest):
    """A project's name and what it is about; only what is sent changes."""

    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=8192)

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("A project needs a name")
        return cleaned


class ProjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str
    status: str
    # The business line this project's work is for, if any.
    venture_id: UUID | None = None
    # Who put it there ("owner" or "office"), and the office's last judgment.
    venture_set_by: str | None = None
    venture_suggestion: dict[str, Any] = Field(
        default_factory=dict, validation_alias="venture_suggestion_json"
    )
    created_at: datetime


class VentureCreate(StrictRequest):
    """One business line: what it is, and how work is judged inside it."""

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4096)
    doctrine: str = Field(default="", max_length=4096)
    role_doctrine: dict[str, str] | None = ROLE_DOCTRINE
    # The skills in this line's pack, offered only to its tasks.
    skills: list[str] | None = Field(default=None, max_length=64)


class VentureUpdate(StrictRequest):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    doctrine: str | None = Field(default=None, max_length=4096)
    role_doctrine: dict[str, str] | None = ROLE_DOCTRINE
    status: str | None = Field(default=None, pattern="^(ACTIVE|ARCHIVED)$")
    skills: list[str] | None = Field(default=None, max_length=64)


class ProjectDeliverable(StrictRequest):
    """What this project's tasks should come to unless a task says otherwise."""

    shape: str = Field(min_length=1, max_length=32)
    format: str = Field(min_length=1, max_length=16)


class ProjectVenture(StrictRequest):
    """Which business line a project's work is for. None detaches it."""

    venture_id: UUID | None = None


class EntityCreate(StrictRequest):
    """A recurring subject of the work: a regulator, an instrument, a vendor."""

    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(default="subject", max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    venture_id: UUID | None = None


class EntityUpdate(StrictRequest):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    kind: str | None = Field(default=None, max_length=32)
    aliases: list[str] | None = Field(default=None, max_length=12)
    status: str | None = Field(default=None, pattern="^(ACTIVE|ARCHIVED)$")


class ObjectiveCreate(StrictRequest):
    """What a business line is trying to achieve, in a window."""

    title: str = Field(min_length=1, max_length=255)
    measure: str = Field(default="", max_length=2000)
    starts_on: date | None = None
    ends_on: date | None = None


class ObjectiveUpdate(StrictRequest):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    measure: str | None = Field(default=None, max_length=2000)
    starts_on: date | None = None
    ends_on: date | None = None
    status: str | None = Field(default=None, pattern="^(OPEN|MET|MISSED|DROPPED)$")


class TemplateCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    project_id: UUID
    objective: str = Field(min_length=1, max_length=8192)
    budget: RunBudget | None = None


class TemplateFromRun(StrictRequest):
    name: str = Field(min_length=1, max_length=255)


class ScheduleCreate(StrictRequest):
    template_id: UUID
    timezone: str = Field(min_length=1, max_length=64)
    frequency: str = Field(pattern="^(daily|weekly)$")
    local_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6)
    # A chat channel each run's result is posted to; None keeps it in the office.
    post_to: UUID | None = None


class ArchiveProtectionSave(StrictRequest):
    """How AI history is protected. `providers` null means every provider."""

    agents: Literal["off", "discussions", "discussions_and_slack"]
    providers: list[str] | None
    backups: Literal["encrypted", "left_out"]


class ArchiveReadStart(StrictRequest):
    """A paid model call over one conversation, so only on a confirmed press."""

    confirm: bool = False


class ArchiveMemorySave(StrictRequest):
    """Keep or reject a proposed memory, or correct its words."""

    status: Literal["kept", "rejected"] | None = None
    content: str | None = Field(default=None, max_length=600)


class ArchiveConversationSave(StrictRequest):
    """Whether agents may reach an imported conversation."""

    excluded: bool


class FollowUpSave(StrictRequest):
    """What the owner says about a to-do."""

    status: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    next_on: date | None = None


class RoutineSave(StrictRequest):
    """A routine set or changed from its page; what is not sent stays as it was."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    quiet_when: str | None = Field(default=None, max_length=1000)
    frequency: str | None = Field(default=None, pattern="^(daily|weekly|hours|cron|once)$")
    local_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6)
    every_hours: int | None = Field(default=None, ge=1, le=168)
    cron: str | None = Field(default=None, max_length=64)
    once_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    # The discussion it answers in; a new one, in this project if given, when none.
    topic_id: UUID | None = None
    project_id: UUID | None = None
    post_to: UUID | None = None
    preset: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None


class SchedulePost(StrictRequest):
    post_to: UUID | None = None
