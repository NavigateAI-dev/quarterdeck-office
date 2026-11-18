from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.approvals import ApprovalScope
from app.runtimes.engineer import EngineeringResult
from app.runtimes.publish import Publisher
from app.tools.forge import ChangeRequest, ForgeError

TOKEN = "glpat-fixture-token-value-long"


def scope(**overrides: Any) -> ApprovalScope:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "project_id": uuid4(),
        "objective": "Add an export endpoint",
        "recommendation": "r",
        "proposed_engineering_scope": "Add app/export.py",
        "permitted_paths": ["app/export.py"],
        "repository": "/srv/repo",
        "base_branch": "main",
    }
    return ApprovalScope(**(base | overrides))


class RecordingForge:
    def __init__(self, error: ForgeError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def publish(
        self, branch: str, title: str, description: str, base_branch: str
    ) -> ChangeRequest:
        if self.error is not None:
            raise self.error
        self.calls.append((branch, base_branch))
        return ChangeRequest(
            url="https://gitlab.com/g/p/-/merge_requests/1",
            identifier="1",
            branch=branch,
            created=True,
        )


def test_a_result_reports_the_published_url() -> None:
    result = EngineeringResult(uuid4(), True, "completed", branch="b", test_exit_code=0)
    result.pull_request_url = "https://gitlab.com/g/p/-/merge_requests/1"
    report = result.report()
    assert report.pull_request_url == "https://gitlab.com/g/p/-/merge_requests/1"
    assert report.status == "completed"


def test_a_publication_failure_preserves_the_engineering_result() -> None:
    """The work is done either way; publication failure is reported alongside."""
    result = EngineeringResult(uuid4(), True, "completed", branch="b", test_exit_code=0)
    result.publication_error = "publication_failed:forge_unauthorized"
    report = result.report()
    assert report.status == "completed"
    assert report.tests.status == "passed"
    assert report.errors == ["publication_failed:forge_unauthorized"]
    assert report.pull_request_url is None


def test_an_unpublished_result_reports_no_url() -> None:
    result = EngineeringResult(uuid4(), True, "completed", branch="b", test_exit_code=0)
    assert result.report().pull_request_url is None


def test_the_base_branch_defaults_when_blank() -> None:
    assert scope(base_branch="").base_branch_name == "main"
    assert scope(base_branch="develop").base_branch_name == "develop"


def settings_for(**overrides: object) -> Any:
    from app.config import Settings

    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://x:y@localhost/x",
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


def test_publication_is_off_unless_enabled_and_configured() -> None:
    from app.runtime.worker import build_publisher

    assert build_publisher(settings_for()) is None
    configured = settings_for(
        enable_github_pr=True,
        gitlab_project_path="navigate-group/quarterdeck",
        gitlab_token=TOKEN,
    )
    assert isinstance(build_publisher(configured), Publisher)


def test_enabling_publication_without_a_target_is_refused() -> None:
    with pytest.raises(ValueError, match="GITLAB_PROJECT_PATH and GITLAB_TOKEN"):
        settings_for(enable_github_pr=True)


def test_a_short_token_is_refused() -> None:
    with pytest.raises(ValueError, match="project access token"):
        settings_for(gitlab_token="too-short")


class PushlessPublisher(Publisher):
    """Publishes without touching git, to test the forge call alone."""

    async def commit(self, repository: Path, message: str) -> bool:
        return True

    async def push(self, repository: Path, branch: str) -> None:
        return None


@pytest.mark.asyncio
async def test_changes_are_committed_before_they_are_pushed(tmp_path: Path) -> None:
    """The runtime edits the working tree and commits nothing, so an
    uncommitted change would publish a branch containing no changes."""
    order: list[str] = []

    class Recording(Publisher):
        async def commit(self, repository: Path, message: str) -> bool:
            order.append("commit")
            return True

        async def push(self, repository: Path, branch: str) -> None:
            order.append("push")

    publisher = Recording(
        forge=RecordingForge(),  # type: ignore[arg-type]
        base_url="https://gitlab.com",
        project_path="navigate-group/quarterdeck",
        token=TOKEN,
    )
    await publisher.publish(tmp_path, "branch", "T", "B", "main")
    assert order == ["commit", "push"]


@pytest.mark.asyncio
async def test_publication_targets_the_approved_base_branch(tmp_path: Path) -> None:
    forge = RecordingForge()
    publisher = PushlessPublisher(
        forge=forge,  # type: ignore[arg-type]
        base_url="https://gitlab.com",
        project_path="navigate-group/quarterdeck",
        token=TOKEN,
    )
    change = await publisher.publish(tmp_path, "quarterdeck/attempt-1", "T", "B", "develop")
    assert forge.calls == [("quarterdeck/attempt-1", "develop")]
    assert change.created


def test_a_push_failure_is_reported_without_the_token() -> None:
    """Push output can echo the remote URL, which carries the credential."""
    from app.tools.forge import redact

    failure = f"fatal: authentication failed for https://oauth2:{TOKEN}@gitlab.com/g/p.git"
    assert TOKEN not in redact(failure, TOKEN)
