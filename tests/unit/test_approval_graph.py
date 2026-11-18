from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.approvals import ApprovalScope
from app.orchestrator.engineering import DisabledEngineering
from app.orchestrator.graph import run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import RunStatus
from app.policy import PermissionDenied
from tests.unit.test_graph import agents, critique, start


def scope(**overrides: Any) -> ApprovalScope:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "project_id": uuid4(),
        "objective": "Assess demand",
        "recommendation": "r",
        "proposed_engineering_scope": "Add an export endpoint",
        "permitted_paths": ["app/export.py"],
        # An engineering target is configured unless a test says otherwise.
        "repository": "/repos/app",
    }
    return ApprovalScope(**(base | overrides))


class Gate:
    """Records the request; the decision is supplied as if by a human."""

    def __init__(self, verdict: str | None, subject: ApprovalScope | None = None) -> None:
        self.verdict = verdict
        self.scope = subject or scope()
        # Set to simulate the proposal changing after the owner saw it.
        self.scope_at_decision: ApprovalScope | None = None
        self.opened = 0

    def open(self, objective: str, synthesis: dict[str, Any]) -> tuple[str, str]:
        self.opened += 1
        return "approval-1", self.scope.digest()

    def decision_for(self, approval_id: str) -> tuple[str, ApprovalScope] | None:
        if self.verdict is None:
            return None
        return self.verdict, self.scope_at_decision or self.scope


async def execute(verdict: str | None, **kwargs: Any) -> tuple[Any, Gate, DisabledEngineering]:
    gate = Gate(verdict, kwargs.get("subject"))
    executor = DisabledEngineering()
    graph = build_graph(agents([critique("PASS")])[0], 3, gate=gate, executor=executor)
    state = await run_analysis(graph, start())
    return state, gate, executor


async def test_approval_stops_the_run_and_records_the_request() -> None:
    state, gate, boundary = await execute(None)
    assert state["status"] == RunStatus.WAITING_FOR_APPROVAL
    assert state["approval_id"] == "approval-1"
    assert gate.opened == 1
    assert boundary.invocations == 0


async def test_rejection_never_invokes_engineering() -> None:
    state, _, boundary = await execute("REJECTED")
    assert state["status"] == RunStatus.REJECTED
    assert state["stopped_reason"] == "rejected_by_owner"
    assert boundary.invocations == 0


async def test_approval_invokes_engineering_exactly_once() -> None:
    state, _, boundary = await execute("APPROVED")
    # Disabled execution produced no code, so the run must not claim success.
    assert state["status"] == RunStatus.FAILED
    assert state["stopped_reason"] == "engineering_failed"
    assert boundary.invocations == 1
    assert state["engineering"]["errors"] == ["code_execution_disabled"]
    assert state["engineering"]["status"] == "failed"


async def test_a_changed_scope_after_approval_is_not_executed() -> None:
    gate = Gate("APPROVED")
    executor = DisabledEngineering()
    graph = build_graph(agents([critique("PASS")])[0], 3, gate=gate, executor=executor)
    # The proposal changes between being shown and being acted on.
    gate.scope_at_decision = gate.scope.model_copy(
        update={"proposed_engineering_scope": "Rewrite billing"}
    )
    state = await run_analysis(graph, start())
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "scope_changed_after_approval"
    assert executor.invocations == 0


@pytest.mark.asyncio
async def test_the_executor_refuses_without_approval() -> None:
    with pytest.raises(PermissionDenied, match="approval_required"):
        await DisabledEngineering().execute(scope(), approved=False)


@pytest.mark.asyncio
async def test_a_pull_request_permission_is_checked_separately() -> None:
    report = await DisabledEngineering().execute(scope(allows_pull_request=True), approved=True)
    assert report["pull_request_url"] is None


@pytest.mark.asyncio
async def test_nothing_executes_while_code_execution_is_disabled() -> None:
    report = await DisabledEngineering().execute(scope(), approved=True)
    assert report["status"] == "failed"
    assert report["errors"] == ["code_execution_disabled"]
    assert report["tests"]["status"] == "not_run"
    assert report["changed_files"] == []


def settings_for(**overrides: object) -> Any:
    from app.config import Settings

    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://x:y@localhost/x",
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


def test_code_execution_stays_off_unless_explicitly_enabled() -> None:
    from app.coder.handoff import CoderHandoff
    from app.orchestrator.engineering import DisabledEngineering
    from app.runtime.worker import build_executor

    database = object()
    assert isinstance(build_executor(settings_for(), database), DisabledEngineering)
    enabled = build_executor(settings_for(enable_code_execution=True), database)
    assert isinstance(enabled, CoderHandoff)


@pytest.mark.asyncio
async def test_engineering_without_a_repository_executes_nothing(tmp_path: Path) -> None:
    from app.coder.handoff import CoderHandoff
    from app.runtimes.engineer import LocalExecutor

    executor = CoderHandoff(
        session_factory=object(),  # type: ignore[arg-type]  # never reached
        engineering_root=tmp_path,
        test_executor=LocalExecutor(),
    )
    report = await executor.execute(scope(repository=None), approved=True)
    assert report["errors"] == ["no_repository_configured"]
    assert executor.invocations == 0


def test_permitted_paths_are_operator_configured() -> None:
    """What may change is configuration, never a model's proposal."""
    settings = settings_for(engineering_permitted_paths="app/export.py, app/*.py ")
    assert settings.permitted_paths == ["app/export.py", "app/*.py"]
    assert settings_for().permitted_paths == []


async def test_engineering_ends_with_a_final_report_built_from_evidence() -> None:
    state, gate, _ = await execute("APPROVED")
    report = state["final_report"]
    assert report["outcome"] == "failed"
    assert report["scope_digest"] == gate.scope.digest()
    assert report["objective"] == gate.scope.objective
    assert report["patch_retained"] is False
    assert "code_execution_disabled" in report["unresolved"]
    assert report["tests"] == "not_run"


def test_the_final_report_is_persisted_as_its_own_artifact() -> None:
    from app.orchestrator.runner import ARTIFACT_OF_STAGE

    assert ARTIFACT_OF_STAGE["final_report"] == ("final_report", "application")


def test_a_successful_report_lists_its_evidence() -> None:
    from app.orchestrator.graph import final_report

    subject = scope(known_uncertainties=["Pricing unknown"])
    report = final_report(
        subject,
        {
            "status": "completed",
            "branch": "quarterdeck/attempt-1",
            "changed_files": ["app/export.py"],
            "tests": {"status": "passed", "details": "1 passed"},
            "pull_request_url": "https://gitlab.example/mr/1",
            "errors": [],
            "diff": "+x",
        },
    )
    assert report["outcome"] == "completed"
    assert report["changed_files"] == ["app/export.py"]
    assert report["patch_retained"] is True
    assert report["merge_request"] == "https://gitlab.example/mr/1"
    assert report["unresolved"] == ["Pricing unknown"]


async def test_approving_an_analysis_with_no_engineering_target_records_the_decision() -> None:
    """The brief promises only a recorded decision; the run must say the same."""
    gate = Gate("APPROVED", scope(repository=None))
    executor = DisabledEngineering()
    graph = build_graph(agents([critique("PASS")])[0], 3, gate=gate, executor=executor)
    state = await run_analysis(graph, start())
    assert state["status"] == RunStatus.COMPLETED
    assert executor.invocations == 0
    assert state["final_report"]["outcome"] == "decision_recorded"
    assert "no_engineering_target" in state["final_report"]["unresolved"]


# Approval only when there is something to authorise.


async def run_with(synthesis: dict[str, Any]) -> tuple[Any, Gate]:
    agent_set, roles = agents([critique("PASS")])
    roles["chief"].outputs = [synthesis]
    gate = Gate(None)
    graph = build_graph(agent_set, 3, gate=gate, executor=DisabledEngineering())
    return await run_analysis(graph, start()), gate


async def test_an_answer_proposing_nothing_completes_without_approval() -> None:
    state, gate = await run_with(
        {
            "facts": ["f"],
            "recommendation": "Retail first.",
            "proposes_action": False,
            "proposed_engineering_scope": "No engineering work is authorized.",
        }
    )
    assert state["status"] == RunStatus.COMPLETED
    assert gate.opened == 0


async def test_an_answer_with_no_scope_completes_without_approval() -> None:
    state, gate = await run_with({"facts": ["f"], "recommendation": "Retail first."})
    assert state["status"] == RunStatus.COMPLETED and gate.opened == 0


async def test_an_answer_proposing_an_action_waits_for_approval() -> None:
    state, gate = await run_with(
        {
            "facts": ["f"],
            "recommendation": "Build the export.",
            "proposes_action": True,
            "proposed_engineering_scope": "Add an export endpoint",
        }
    )
    assert state["status"] == RunStatus.WAITING_FOR_APPROVAL and gate.opened == 1


async def test_an_answer_recorded_before_this_still_waits() -> None:
    """No proposes_action: it behaves as it did when it was written."""
    state, gate = await run_with(
        {"facts": ["f"], "recommendation": "r", "proposed_engineering_scope": "Add an export"}
    )
    assert state["status"] == RunStatus.WAITING_FOR_APPROVAL and gate.opened == 1


# Recovery re-enters at the approval node; an approval is carried out once.


class LedgerGate(Gate):
    """A gate that also keeps the execution ledger, as the database gate does."""

    def __init__(self, verdict: str | None, ledger: dict[str, dict[str, Any]]) -> None:
        super().__init__(verdict)
        self.ledger = ledger

    def begin_execution(self, approval_id: str, digest: str) -> tuple[str, dict[str, Any] | None]:
        entry = self.ledger.get(approval_id)
        if entry is None:
            self.ledger[approval_id] = {"status": "STARTED", "report": None}
            return "new", None
        if entry["status"] == "FINISHED":
            return "finished", entry["report"]
        return "in_flight", None

    def finish_execution(self, approval_id: str, report: dict[str, Any]) -> None:
        self.ledger[approval_id] = {"status": "FINISHED", "report": report}


async def run_ledgered(ledger: dict[str, dict[str, Any]], executor: DisabledEngineering) -> Any:
    gate = LedgerGate("APPROVED", ledger)
    graph = build_graph(agents([critique("PASS")])[0], 3, gate=gate, executor=executor)
    return await run_analysis(graph, start())


async def test_recovery_after_engineering_does_not_run_it_again() -> None:
    ledger: dict[str, dict[str, Any]] = {}
    executor = DisabledEngineering()
    first = await run_ledgered(ledger, executor)
    # The process died before the run was finalized; recovery replays approval.
    again = await run_ledgered(ledger, executor)
    assert executor.invocations == 1, "the approved work ran a second time"
    assert again["engineering"] == first["engineering"]
    assert ledger["approval-1"]["status"] == "FINISHED"


async def test_an_attempt_interrupted_mid_flight_waits_for_the_owner() -> None:
    ledger: dict[str, dict[str, Any]] = {"approval-1": {"status": "STARTED", "report": None}}
    executor = DisabledEngineering()
    state = await run_ledgered(ledger, executor)
    assert executor.invocations == 0, "an uncertain outcome is not retried blindly"
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "engineering_outcome_uncertain"
