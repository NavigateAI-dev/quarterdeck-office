from dataclasses import dataclass
from typing import Any, Protocol

from app.approvals import ApprovalScope
from app.policy import require
from app.schemas.engineering import EngineeringReport, EngineeringStatus, TestReport, TestStatus


class EngineeringExecutor(Protocol):
    """Acts on an approved scope. Authorization is checked by the caller."""

    invocations: int

    async def execute(self, scope: ApprovalScope, approved: bool) -> dict[str, Any]: ...


@dataclass
class DisabledEngineering:
    """The default. Records what was authorized without executing anything.

    Approval still had to happen to reach this point, so the permission checks
    run exactly as they would with a live runtime.
    """

    invocations: int = 0

    async def execute(self, scope: ApprovalScope, approved: bool) -> dict[str, Any]:
        require("modify_code", approved=approved)
        if scope.allows_pull_request:
            require("create_pull_request", approved=approved)
        self.invocations += 1
        return EngineeringReport(
            status=EngineeringStatus.FAILED,
            tests=TestReport(status=TestStatus.NOT_RUN, details="code_execution_disabled"),
            summary=scope.proposed_engineering_scope,
            errors=["code_execution_disabled"],
        ).model_dump()
