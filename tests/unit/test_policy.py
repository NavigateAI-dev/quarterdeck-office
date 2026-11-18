from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.approvals import ApprovalScope, ArtifactRef, build_brief, build_scope, content_digest
from app.policy import Permission, PermissionDenied, permission_for, require


@pytest.mark.parametrize(
    "action",
    [
        "send_external_email",
        "deploy_production",
        "merge_pull_request",
        "spend_money",
        "delete_external_data",
    ],
)
def test_denied_actions_cannot_be_approved_into_existence(action: str) -> None:
    for approved in (False, True):
        with pytest.raises(PermissionDenied, match="denied_by_policy"):
            require(action, approved=approved, preapproved=True)


@pytest.mark.parametrize("action", ["web_search", "read_project_files", "write_project_artifacts"])
def test_auto_actions_need_no_approval(action: str) -> None:
    require(action)


@pytest.mark.parametrize("action", ["modify_code", "create_decision_record"])
def test_approval_gated_actions_require_approval(action: str) -> None:
    with pytest.raises(PermissionDenied, match="approval_required"):
        require(action)
    require(action, approved=True)


def test_preapproval_only_applies_where_the_policy_allows_it() -> None:
    require("create_pull_request", preapproved=True)
    with pytest.raises(PermissionDenied, match="approval_required"):
        require("modify_code", preapproved=True)


def test_unknown_actions_are_denied() -> None:
    with pytest.raises(PermissionDenied, match="unknown_action"):
        permission_for("rm_minus_rf")


def test_policy_matches_the_specification_table() -> None:
    assert permission_for("create_pull_request") is Permission.APPROVAL_OR_PREAPPROVED
    assert permission_for("modify_code") is Permission.APPROVAL


def scope(**overrides: object) -> ApprovalScope:
    base: dict[str, object] = {
        "run_id": uuid4(),
        "project_id": uuid4(),
        "objective": "Assess demand",
        "recommendation": "Build a narrow prototype",
        "proposed_engineering_scope": "Add a research export endpoint",
        "evidence": [
            ArtifactRef(
                artifact_id=uuid4(),
                type="synthesis",
                version=1,
                content_digest=content_digest("body"),
            )
        ],
    }
    return ApprovalScope(**(base | overrides))  # type: ignore[arg-type]


def test_identical_proposals_digest_identically() -> None:
    first = scope()
    assert first.digest() == first.model_copy().digest()


def test_digest_changes_when_the_proposal_changes() -> None:
    first = scope()
    assert first.digest() != scope(**{"proposed_engineering_scope": "Rewrite billing"}).digest()


def test_digest_changes_when_bound_evidence_changes() -> None:
    first = scope()
    changed = first.model_copy(
        update={
            "evidence": [
                first.evidence[0].model_copy(update={"content_digest": content_digest("other")})
            ]
        }
    )
    assert first.digest() != changed.digest()


def test_digest_changes_when_an_artifact_version_changes() -> None:
    first = scope()
    changed = first.model_copy(
        update={"evidence": [first.evidence[0].model_copy(update={"version": 2})]}
    )
    assert first.digest() != changed.digest()


def test_scope_is_immutable() -> None:
    with pytest.raises(ValidationError):
        scope().proposed_engineering_scope = "something else"  # type: ignore[misc]


def test_brief_reports_the_digest_the_owner_is_approving() -> None:
    subject = scope()
    brief = build_brief(subject, {"key_findings": ["f"]}, {"critical_issues": ["c"]})
    assert brief.scope_digest == subject.digest()
    assert brief.research_summary == ["f"]
    assert brief.critical_review == ["c"]


def test_scope_is_built_from_the_synthesis() -> None:
    built = build_scope(
        uuid4(),
        uuid4(),
        "Assess demand",
        {"recommendation": "r", "proposed_engineering_scope": "s", "unknowns": ["u"]},
        [],
    )
    assert (built.recommendation, built.proposed_engineering_scope) == ("r", "s")
    assert built.known_uncertainties == ["u"]
    assert built.allows_pull_request is False


ENGINEERING = {
    "repository": "/repos/app",
    "base_branch": "release",
    "base_commit": "a" * 40,
    "permitted_paths": ["app/*.py"],
    "test_command": "pytest -q",
}


def test_the_brief_says_nothing_runs_without_an_engineering_target() -> None:
    brief = build_brief(scope(), None, None)
    assert "no code is executed" in brief.expected_output.lower()
    assert brief.executes_code is False


def test_the_brief_says_nothing_runs_when_execution_is_disabled() -> None:
    brief = build_brief(scope(**ENGINEERING, executes_code=False), None, None)
    assert "code execution is disabled" in brief.expected_output.lower()


def test_the_brief_names_what_approval_will_execute() -> None:
    """Finding 6: the owner must see the actual consequence of approving."""
    brief = build_brief(scope(**ENGINEERING, executes_code=True), None, None)
    text = brief.expected_output
    assert "/repos/app" in text
    assert "release" in text
    assert "a" * 12 in text
    assert "app/*.py" in text
    assert "pytest -q" in text
    assert "no branch is published" in text.lower()
    assert brief.executes_code is True
    assert brief.repository == "/repos/app"
    assert brief.test_command == "pytest -q"
    assert "no code is executed" not in text.lower()


def test_the_brief_discloses_publication() -> None:
    brief = build_brief(
        scope(**ENGINEERING, executes_code=True, allows_pull_request=True), None, None
    )
    assert "merge request" in brief.expected_output.lower()
    assert brief.publishes is True


def test_execution_permission_is_part_of_the_digest() -> None:
    subject = scope(executes_code=False)
    assert subject.digest() != subject.model_copy(update={"executes_code": True}).digest()
