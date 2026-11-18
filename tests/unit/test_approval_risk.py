"""Which proposals may be approved from chat: only those whose approval records a decision."""

from uuid import uuid4

import pytest

from app.approvals import ApprovalScope, low_risk, risk_reason


def scope(**changes: object) -> ApprovalScope:
    return ApprovalScope(
        run_id=uuid4(),
        project_id=uuid4(),
        objective="Pick a CRM",
        recommendation="CRM B",
        proposed_engineering_scope="None",
        **changes,  # type: ignore[arg-type]
    )


def test_a_proposal_that_only_records_a_decision_is_low_risk() -> None:
    assert low_risk(scope()) and risk_reason(scope()) is None


def test_a_repository_with_code_execution_switched_off_is_still_low_risk() -> None:
    assert low_risk(scope(repository="acme/site", permitted_paths=["src/"]))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"lands_in_source": True}, "writes code into a folder or repository"),
        ({"lands_on_branch": "office/x"}, "writes code into a folder or repository"),
        ({"executes_code": True, "repository": "acme/site"}, "runs code"),
        ({"allows_pull_request": True}, "may publish a branch"),
        ({"installs_packages": True}, "installs packages"),
    ],
)
def test_anything_that_runs_writes_installs_or_publishes_is_not(
    changes: dict[str, object], reason: str
) -> None:
    assert not low_risk(scope(**changes))
    assert risk_reason(scope(**changes)) == reason
