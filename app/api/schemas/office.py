"""API request and response shapes: office."""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from app.api.schemas.base import StrictRequest


class PolicyCreate(StrictRequest):
    """Turn one of the office's rules on."""

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    scope: str = Field(default="office", pattern="^(office|project)$")
    project_id: UUID | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(StrictRequest):
    """The owner's answer to a rule that paused a task."""

    allow: bool


class PolicyUpdate(StrictRequest):
    params: dict[str, Any] | None = None
    enabled: bool | None = None


class AccessAddress(StrictRequest):
    """An outside address the owner chose for a phone."""

    address: str = Field(min_length=8, max_length=255)


class WidgetChange(StrictRequest):
    """A widget's size on the wall, or whether agents read it."""

    size: Literal["wide", "small", "folded"] | None = None
    agents_read: bool | None = None


class WidgetRemove(StrictRequest):
    """Off the wall; with clean, what it knew is deleted too."""

    clean: bool = False


class WidgetOrder(StrictRequest):
    kinds: list[str] = Field(max_length=40)


class BuildRestore(StrictRequest):
    """Bring an earlier version back as a new version."""

    commit: str = Field(min_length=7, max_length=40, pattern="^[0-9a-f]+$")


class BenchmarkStart(StrictRequest):
    """The owner's press to run the benchmark, knowing it uses paid model calls."""

    confirm: Literal[True]


class BuildNote(StrictRequest):
    """The owner's word to a build while it is written."""

    mode: Literal["next", "now", "pause", "continue"] = "next"
    text: str | None = Field(default=None, max_length=4000)
    # What the owner pointed at on the live page, as the page reported it.
    pointed: dict[str, str] | None = None

    @model_validator(mode="after")
    def note_needs_words(self) -> "BuildNote":
        if self.mode in ("next", "now") and not (self.text or "").strip():
            raise ValueError("Say what to change")
        return self


class PublishRequest(StrictRequest):
    """Put a built website online. The owner confirms it will be public."""

    target: Literal["netlify", "github"]
    public: Literal[True]
    # A version to publish; the latest when left out.
    version: str | None = Field(default=None, min_length=7, max_length=40, pattern="^[0-9a-f]+$")
    name: str | None = Field(default=None, max_length=60)
    token: SecretStr | None = Field(default=None, min_length=8, max_length=400)


class CodingAgentChoice(StrictRequest):
    """Which coding CLI on the owner's Mac codes."""

    agent: Literal["claude_code", "codex", "opencode", "dsh"] | None = None
    # Whether builds may install packages from npm and PyPI.
    packages: bool | None = None


class OwnerLoginSet(StrictRequest):
    """The owner's sign-in name and password. The current password is
    needed to change one that is already set."""

    name: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=1, max_length=256)
    current_password: SecretStr | None = Field(default=None, max_length=256)


class OwnerLoginClear(StrictRequest):
    current_password: SecretStr = Field(min_length=1, max_length=256)


class RemoteConnect(StrictRequest):
    """Joining the tailnet: a one-time auth key, or none for a login link."""

    auth_key: SecretStr | None = Field(default=None, max_length=200)


class AccessPair(StrictRequest):
    """Which outside address a phone should be paired through."""

    kind: Literal["home", "remote"]


class OfficeSettingsUpdate(StrictRequest):
    """The owner's office settings. Null clears one back to configuration."""

    office_name: str | None = Field(default=None, min_length=1, max_length=128)
    owner_name: str | None = Field(default=None, min_length=1, max_length=255)
    # Switches: each stops new work of one kind; running work finishes.
    pause_new_tasks: bool | None = None
    pause_schedules: bool | None = None
    pause_chat: bool | None = None
    advisor_on: bool | None = None
    team_on: bool | None = None
    # The office's own browser.
    browser_on: bool | None = None
    # Tasks run at the same time (MW).
    runs_at_once: int | None = Field(default=None, ge=1, le=8)
    # Related work from another business line.
    related_across_lines: bool | None = None
    # How a task's model is chosen (§16): auto routing, or the fixed role models.
    model_routing: Literal["auto", "fixed"] | None = None

    run_max_tokens: int | None = Field(default=None, gt=0, le=50_000_000)
    run_max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    run_max_minutes: int | None = Field(default=None, gt=0, le=1440)
    monthly_cost_limit_usd: float | None = Field(default=None, gt=0, le=100_000)
    chat_daily_max_tokens: int | None = Field(default=None, ge=0, le=5_000_000)
    # role -> model connection id, or null to use the office's model gateway.
    role_models: dict[str, UUID | None] | None = Field(default=None, max_length=12)
