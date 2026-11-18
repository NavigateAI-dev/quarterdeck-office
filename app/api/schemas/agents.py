"""API request and response shapes: agents."""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.schemas.base import StrictRequest


class MemberAdd(StrictRequest):
    """One more person who may talk to this workspace's bots."""

    user_id: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class SkillSource(StrictRequest):
    """A skill from skills.sh or GitHub, as a person would name it."""

    source: str = Field(min_length=1, max_length=400)


class SkillSourceInstall(SkillSource):
    """Install what was previewed: that commit, for that role."""

    commit: str = Field(pattern="^[0-9a-f]{40}$")
    role: str = Field(pattern="^(researcher|strategist|summarist|critic|chief)$")


class SkillImport(StrictRequest):
    """A SKILL.md written by another harness, as text the owner pasted or read."""

    text: str = Field(min_length=1, max_length=20_000)


class MemoryDecision(StrictRequest):
    """The owner's word on one candidate lesson, and where it applies."""

    approved: bool
    scope: str = Field(default="project", pattern="^(project|venture|office)$")


class SkillPin(StrictRequest):
    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class SkillInstall(SkillPin):
    pass


class AgentVersionCreate(StrictRequest):
    instructions: str = Field(default="", max_length=32768)
    connection_id: UUID | None = None
    limits: dict[str, int] = Field(default_factory=dict, max_length=16)
    skills: list[SkillPin] = Field(default_factory=list, max_length=10)


class AgentCreate(AgentVersionCreate):
    name: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")

    @field_validator("role", mode="before")
    @classmethod
    def role_as_a_name(cls, value: object) -> object:
        """A role written in words is kept as its name: "Slac communication
        assistant" is slac-communication-assistant. It used to be refused with
        the pattern as the only explanation."""
        if not isinstance(value, str):
            return value
        words = re.sub(r"[^a-z0-9_]+", "-", value.strip().lower()).strip("-_")
        return re.sub(r"^[^a-z]+", "", words)[:64].strip("-_")


class AgentImport(StrictRequest):
    """An agent file, and whether to act on it or only report what it would do."""

    document: str = Field(min_length=1, max_length=200_000)
    confirm: bool = False


class AgentIdentityUpdate(StrictRequest):
    """Rename an agent, or change which role it fills."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"
    )


class AgentVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    instructions: str
    connection_id: UUID | None
    limits: dict[str, Any]
    skills: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class AgentView(BaseModel):
    id: UUID
    name: str
    role: str
    status: str
    enabled_version: int | None
    versions: list[AgentVersionView]
    created_at: datetime
    updated_at: datetime
