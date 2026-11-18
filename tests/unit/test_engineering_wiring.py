"""Boundary wiring between the worker and the executor that hands coding to the Mac."""

from pathlib import Path
from uuid import uuid4

from app.coder.handoff import CoderHandoff
from app.config import Settings
from app.orchestrator.engineering import DisabledEngineering
from app.runtime.worker import build_executor
from app.runtimes.engineer import EngineeringResult, LocalExecutor
from tests.unit.test_approval_graph import scope


def test_the_report_retains_the_patch() -> None:
    """The workspace is discarded after the attempt; the diff must survive it."""
    result = EngineeringResult(
        uuid4(), True, "completed", changed_paths=["a.py"], diff="+print('x')\n"
    )
    assert result.report().diff == "+print('x')\n"


async def test_an_approval_that_ran_no_code_cannot_authorize_execution_later(
    tmp_path: Path,
) -> None:
    executor = CoderHandoff(
        session_factory=object(),  # type: ignore[arg-type]  # never reached
        engineering_root=tmp_path,
        test_executor=LocalExecutor(),
    )
    report = await executor.execute(
        scope(repository=str(tmp_path), executes_code=False), approved=True
    )
    assert report["status"] == "failed"
    assert report["errors"] == ["approval_does_not_cover_execution"]
    assert executor.invocations == 0


def test_the_runtime_receives_the_approved_task_contract() -> None:
    """Medium finding: execution gets the structured contract, not prose alone."""
    from app.runtimes.engineer import engineering_task, render_instructions

    approved = scope(
        repository="/repos/app",
        base_branch="release",
        test_command="pytest -q",
        acceptance_criteria=["Export returns CSV", "Existing tests pass"],
    )
    task = engineering_task(approved)
    assert task.approved is True
    assert task.task_id == str(approved.run_id)
    assert task.acceptance_criteria == ["Export returns CSV", "Existing tests pass"]
    assert task.base_branch == "release"

    text = render_instructions(task)
    assert "Export returns CSV" in text
    assert "app/export.py" in text
    assert "pytest -q" in text
    assert "Add no dependencies the rules below do not allow" in text
    assert approved.objective in text


def test_acceptance_criteria_are_part_of_what_is_approved() -> None:
    from app.approvals import build_brief, build_scope

    built = build_scope(
        uuid4(),
        uuid4(),
        "Assess demand",
        {
            "recommendation": "r",
            "proposed_engineering_scope": "s",
            "engineering_acceptance_criteria": ["Export returns CSV"],
        },
        [],
    )
    assert built.acceptance_criteria == ["Export returns CSV"]
    assert build_brief(built, None, None).acceptance_criteria == ["Export returns CSV"]
    changed = built.model_copy(update={"acceptance_criteria": ["Something else"]})
    assert built.digest() != changed.digest()


def test_production_runs_repository_tests_in_the_isolated_runner() -> None:
    from app.runtimes.isolated import IsolatedExecutor

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://office:secret@localhost/office",
        enable_code_execution=True,
        engineering_workspace="/engineering",
    )
    executor = build_executor(settings, session_factory=object())
    assert isinstance(executor, CoderHandoff)
    assert isinstance(executor.test_executor, IsolatedExecutor)
    assert executor.test_executor.root == Path("/engineering")
    assert executor.engineering_root == Path("/engineering")


def test_without_the_database_nothing_codes() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://office:secret@localhost/office",
        enable_code_execution=True,
    )
    assert isinstance(build_executor(settings), DisabledEngineering)


def test_with_the_database_coding_is_handed_to_the_coder_on_the_mac(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost:5432/test",
        owner_api_token="owner-token-of-sufficient-length",
        workspace_root=tmp_path,
        enable_code_execution=True,
    )
    executor = build_executor(settings, session_factory=object())
    assert isinstance(executor, CoderHandoff)
