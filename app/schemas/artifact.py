import json
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator

from app.schemas._base import StrictModel
from app.schemas.task import AgentRole


class ArtifactType(StrEnum):
    RESEARCH_REPORT = "research_report"
    MARKET_ANALYSIS = "market_analysis"
    PRODUCT_PROPOSAL = "product_proposal"
    PRD = "prd"
    CRITIQUE = "critique"
    DECISION_BRIEF = "decision_brief"
    ENGINEERING_PLAN = "engineering_plan"
    CODE_CHANGE = "code_change"
    TEST_REPORT = "test_report"
    PULL_REQUEST = "pull_request"


class ArtifactMetadata(StrictModel):
    """Filesystem artifact metadata from SPEC section 12."""

    artifact_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    type: ArtifactType
    created_by: AgentRole
    created_at: datetime
    path: str
    version: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("artifact_id", "project_id", "task_id", "run_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not all(character.isalnum() or character in "._-" for character in value):
            raise ValueError(
                "identifiers may contain only letters, numbers, dots, dashes and underscores"
            )
        return value

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.name or "\\" in value:
            raise ValueError("path must be a safe relative POSIX path")
        return path.as_posix()

    @field_validator("metadata")
    @classmethod
    def validate_json_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("metadata must be JSON serializable") from None
        return value
