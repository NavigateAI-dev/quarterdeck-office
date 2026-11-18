from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas._base import StrictModel


class EngineeringStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class TestReport(StrictModel):
    status: TestStatus
    details: str = ""


class EngineeringTask(StrictModel):
    """The approved unit of engineering work (SPEC section 20).

    Built from a recorded approval, never from a request body: `approved` is
    derived from the approval record, so a submitted flag cannot authorize work.
    """

    task_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    base_branch: str = Field(min_length=1, max_length=255)
    objective: str = Field(min_length=1)
    requirements_artifact: str | None = None
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    permitted_paths: list[str] = Field(default_factory=list)
    test_command: str | None = None
    approved: bool = False


class EngineeringReport(BaseModel):
    """What the adapter returns (SPEC section 20).

    Evidence assembled by the application from the diff and the test run, not
    copied from what the runtime claimed about its own work.
    """

    model_config = ConfigDict(extra="forbid")

    status: EngineeringStatus
    branch: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    tests: TestReport = Field(default_factory=lambda: TestReport(status=TestStatus.NOT_RUN))
    pull_request_url: str | None = None
    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    # The patch against the approved base, as a preview. The attempt clone is
    # discarded afterwards; the whole patch is kept as a file, named
    # here with its digest, so a large change is never cut short.
    diff: str = ""
    diff_truncated: bool = False
    # A build committed into its project folder, and the version it is.
    landed: bool = False
    commit: str | None = None
    patch_path: str | None = None
    patch_sha256: str | None = None
