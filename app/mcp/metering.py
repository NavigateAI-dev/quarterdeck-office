"""Paid tool calls, counted against the limits the owner set.

MCP has no standard way for a server to say what a call costs, so the owner
says it when reviewing a tool: the most one call costs. A priced call passes
two checks before it is sent:

- **the task's cost ceiling**: a call whose price would pass it is not sent,
  and the task stops with the same question a model call at the ceiling asks
  The owner can raise the ceiling and continue.
- **the monthly limits**, the office's and the project's: once one is reached
  the call is refused and the task goes on without that tool. They bound new
  spending, and a tool is optional material.

After it is sent, the price is recorded as usage, so every total the office
keeps (the task, the project, the month, the usage report) counts it, and the
run's ledger rises with it. A call is charged once it was sent, whatever came
back: the office cannot see whether the vendor billed a failure, and counting
it as free would understate what was spent.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from uuid import UUID

from sqlalchemy.orm import Session

from app.budget import RunLedger
from app.config import Settings
from app.db.models import Project
from app.events import InvocationEvent
from app.gateway.client import GatewayError
from app.office_settings import MonthlyLimitReached, check_monthly_limit
from app.project_settings import spent_this_month

# How a tool's spending is recorded beside the models'.
ROLE = "tools"
PROVIDER = "mcp"
# Failures that happen before the call leaves the office, or before the server
# would do any work for it: nothing was sent that a vendor could bill.
NOT_SENT = frozenset(
    {
        "non_public_address",
        "host_unresolvable",
        "missing_host",
        "scheme_not_allowed",
        "host_not_allowed",
        "unauthorised",
        "sign_in_required",
    }
)


class ToolMeter:
    """Checks a priced call against the limits, and records what it cost."""

    def __init__(
        self,
        ledger: RunLedger | None,
        record: Callable[[InvocationEvent], None],
        refusal: Callable[[], str | None],
        *,
        run_id: UUID | None,
        task_id: UUID | None,
        project_id: UUID,
    ) -> None:
        self.ledger = ledger
        self.record = record
        # Why new spending is refused now, if it is: a monthly limit reached.
        self.refusal = refusal
        self.run_id = run_id
        self.task_id = task_id
        self.project_id = project_id

    def admit(self, price: float) -> str | None:
        """Before sending. Returns why the call is refused, or raises the
        budget stop when the task's cost ceiling would be passed."""
        if self.ledger is not None:
            ceiling = self.ledger.budget.max_cost_usd
            if ceiling is not None and self.ledger.spent_cost + price > ceiling:
                self.ledger.exhausted = "cost"
                raise GatewayError("budget_exhausted:cost")
        return self.refusal()

    def charge(self, server: str, tool: str, price: float, outcome: str, started: float) -> None:
        """After sending: the price is spent, whatever came back."""
        if self.ledger is not None:
            self.ledger.spent_cost += price
        self.record(
            InvocationEvent(
                run_id=self.run_id,
                task_id=self.task_id,
                project_id=self.project_id,
                role=ROLE,
                provider=PROVIDER,
                model_alias=f"{server}:{tool}"[:64],
                status=outcome[:32] or "ok",
                estimated_cost=price,
                latency_seconds=max(0.0, monotonic() - started),
            )
        )


def spending_refusal(session: Session, settings: Settings, project_id: UUID) -> str | None:
    """Why new spending in this project is refused now, if it is."""
    try:
        check_monthly_limit(session, settings)
    except MonthlyLimitReached:
        return "monthly_limit_reached"
    project = session.get(Project, project_id)
    limit = project.monthly_cost_limit_usd if project else None
    if limit is not None and spent_this_month(session, project_id) >= limit:
        return "project_limit_reached"
    return None
