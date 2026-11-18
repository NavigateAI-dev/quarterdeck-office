from enum import StrEnum


class Permission(StrEnum):
    """How an action may be taken. Enforced in code, never in prompts alone."""

    AUTO = "auto"
    APPROVAL = "approval"
    APPROVAL_OR_PREAPPROVED = "approval_or_preapproved_engineering_run"
    DENY = "deny"


# SPEC section 18. This table is the authority; prompt text is not a control.
POLICY: dict[str, Permission] = {
    "web_search": Permission.AUTO,
    "read_project_files": Permission.AUTO,
    "write_project_artifacts": Permission.AUTO,
    "create_internal_task": Permission.AUTO,
    "modify_strategy_record": Permission.APPROVAL,
    "create_decision_record": Permission.APPROVAL,
    "modify_code": Permission.APPROVAL,
    "create_pull_request": Permission.APPROVAL_OR_PREAPPROVED,
    "send_external_email": Permission.DENY,
    "deploy_production": Permission.DENY,
    "merge_pull_request": Permission.DENY,
    # Merging an office/ branch in a space's folder: the owner's own act.
    "merge_workspace_branch": Permission.APPROVAL,
    "spend_money": Permission.DENY,
    "delete_external_data": Permission.DENY,
}


class PermissionDenied(Exception):
    """The action is not permitted in the state it was attempted."""

    def __init__(self, action: str, reason: str) -> None:
        self.action = action
        self.reason = reason
        super().__init__(f"{action}:{reason}")


def permission_for(action: str) -> Permission:
    if action not in POLICY:
        raise PermissionDenied(action, "unknown_action")
    return POLICY[action]


def require(action: str, *, approved: bool = False, preapproved: bool = False) -> None:
    """Authorize one action.

    A denied action is refused regardless of approval: no human decision and no
    configuration flag can promote it. Approval-gated actions require an
    approval that the caller has already verified against a durable record.
    """
    permission = permission_for(action)
    if permission is Permission.DENY:
        raise PermissionDenied(action, "denied_by_policy")
    if permission is Permission.AUTO:
        return
    if permission is Permission.APPROVAL and not approved:
        raise PermissionDenied(action, "approval_required")
    if permission is Permission.APPROVAL_OR_PREAPPROVED and not (approved or preapproved):
        raise PermissionDenied(action, "approval_required")
