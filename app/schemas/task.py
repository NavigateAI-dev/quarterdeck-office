from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field, field_validator

from app.schemas._base import StrictModel


class TaskType(StrEnum):
    RESEARCH = "research"
    STRATEGY = "strategy"
    CRITIQUE = "critique"
    SYNTHESIS = "synthesis"
    ENGINEERING = "engineering"


class AgentRole(StrEnum):
    CHIEF = "chief"
    RESEARCHER = "researcher"
    STRATEGIST = "strategist"
    CRITIC = "critic"
    ENGINEER = "engineer"


class TaskContract(StrictModel):
    """Validated delegation contract from SPEC section 11."""

    task_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    type: TaskType
    objective: str = Field(min_length=1, max_length=10_000)
    owner: AgentRole
    inputs: list[str]
    acceptance_criteria: list[str]
    expected_artifact: str
    requires_approval: bool

    @field_validator("task_id", "project_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not all(character.isalnum() or character in "._-" for character in value):
            raise ValueError(
                "identifiers may contain only letters, numbers, dots, dashes and underscores"
            )
        return value

    @field_validator("inputs", "acceptance_criteria")
    @classmethod
    def validate_nonempty_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list items must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("list items must be unique")
        return values

    @field_validator("expected_artifact")
    @classmethod
    def validate_artifact_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.name or "\\" in value:
            raise ValueError("expected_artifact must be a safe relative POSIX path")
        return path.as_posix()
