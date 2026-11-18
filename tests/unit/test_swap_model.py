"""swap_model: what a mid-run downgrade keeps and replaces (characterisation).

The worker composes each role's model as budget → guard → failover → client.
These tests pin today's behaviour so the S3 role-stack builder can change it on
purpose rather than by accident.
"""

from types import SimpleNamespace
from typing import Any

from app.agent_routing import swap_model
from app.budget import BudgetedCompleter
from app.failover import FailoverCompleter
from app.policies.guard import GuardedCompleter


class Model:
    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(self, *args: Any, **kwargs: Any) -> str:
        return self.name


def stacked_role(client: Model) -> SimpleNamespace:
    failover = FailoverCompleter(client, key="deepseek-id", backup_key="gateway", make_backup=Model)
    guarded = GuardedCompleter(failover, gate=object(), run_id=None, project_id=None, role="chief")
    budgeted = BudgetedCompleter(guarded, ledger=object())  # type: ignore[arg-type]
    return SimpleNamespace(gateway=budgeted)


def test_swap_keeps_budget_and_guard() -> None:
    role = stacked_role(Model("primary"))
    budgeted = role.gateway
    guarded = budgeted.inner

    swap_model(role, Model("cheaper"))

    assert role.gateway is budgeted
    assert budgeted.inner is guarded


def test_swap_replaces_the_failover_layer_with_the_new_model() -> None:
    """swap_model replaces the whole model, failover layer included.

    Decided 16 Oct 2026: a downgraded role keeps a backup. That is
    ModelStack.backed's job, which wraps the new model before it is swapped in
    (tests/unit/test_model_downgrade.py); swap_model itself stays a plain swap.
    """
    role = stacked_role(Model("primary"))
    cheaper = Model("cheaper")

    swap_model(role, cheaper)

    innermost = role.gateway.inner.inner
    assert innermost is cheaper
    assert not isinstance(innermost, FailoverCompleter)


def test_swap_on_a_bare_role_replaces_the_model() -> None:
    role = SimpleNamespace(gateway=Model("primary"))
    cheaper = Model("cheaper")

    swap_model(role, cheaper)

    assert role.gateway is cheaper
