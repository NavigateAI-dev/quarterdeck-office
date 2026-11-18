"""A task moved to a cheaper model keeps its budget and the office's policies."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.agent_routing import budget, guard, innermost
from app.budget import Budget, RunLedger
from app.failover import GATEWAY, FailoverCompleter
from app.gateway.client import GatewayError
from app.orchestrator.graph import AgentSet
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyRefused

ROLES = ("planner", "researcher", "strategist", "critic", "chief")


class Reached(Exception):
    """The call got through to a model."""


class Provider:
    """A model that counts how often it was reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, model_alias: str, messages: list, *args: Any, **options: Any) -> Any:
        self.calls += 1
        raise Reached


class Role:
    def __init__(self, role: str, gateway: Any) -> None:
        self.role = role
        self.gateway = gateway
        self.provider = ""
        self.model_alias = ""


class Gate:
    def __init__(self, deny: bool = False) -> None:
        self.deny = deny

    def enforce(self, hook: Any, name: str, **facts: Any) -> Decision:
        decision = Decision(verdict=Verdict.ALLOW, reason="test", policy="test")
        if self.deny:
            raise PolicyRefused(Decision(verdict=Verdict.DENY, reason="no", policy="no"), None)
        return decision


class Rows:
    """A session whose one stored connection is a backup the owner may choose."""

    def scalars(self, statement: Any) -> list[Any]:
        return [
            SimpleNamespace(
                id="c-new-backup",
                provider="openai",
                model_id="m",
                base_url=None,
                display_name="New backup",
            )
        ]


class Repository:
    def usage(self, run_id: Any) -> dict[str, Any]:
        return {"spent": {"cost_usd": 4.0, "cost_reported": True}}

    def add_event(self, run_id: Any, kind: str, payload: dict[str, Any]) -> None:
        pass


class NoSession:
    def __enter__(self) -> Any:
        return Rows()

    def __exit__(self, *args: object) -> None:
        return None


def _downgraded(
    monkeypatch: pytest.MonkeyPatch,
    gate: Gate,
    ledger: RunLedger,
    backed: bool = False,
    plans: dict[str, Any] | None = None,
) -> tuple[AgentSet, Provider]:
    """A guarded, budgeted set of roles after the downgrade has fired.

    `backed` gives each role a backup before the downgrade, as the owner's
    provider backups do at task start; `plans` are the owner's backups by key.
    """
    from app.config import get_settings
    from app.orchestrator.runner import Dispatch
    from app.runtime.job import Job
    from app.runtime.worker import RunWorker

    cheap = Provider()
    entry = {"connection_id": str(uuid4()), "provider": "groq", "model_id": "small"}
    monkeypatch.setattr(
        "app.runtime.model_stack.downgrade_rule", lambda session, project_id: (2.5, entry)
    )
    monkeypatch.setattr(
        "app.runtime.model_stack.completer_for_entry", lambda entry, settings, factory: cheap
    )
    monkeypatch.setattr(
        "app.runtime.model_stack.provider_backups",
        lambda session: {
            key.format(cheap=entry["connection_id"]): plan for key, plan in (plans or {}).items()
        },
    )
    monkeypatch.setattr("app.runtime.model_stack.select", lambda *args: None)
    agents = AgentSet(**{name: Role(name, Provider()) for name in ROLES})  # type: ignore[arg-type]
    if backed:
        for name in ROLES:
            role = getattr(agents, name)
            role.gateway = FailoverCompleter(role.gateway, GATEWAY, "c-old-backup", Provider, "old")
    job = Job(
        dispatch=Dispatch(run_id=uuid4(), task_id=uuid4(), project_id=uuid4()),
        objective="o",
        context={},
    )
    guard(agents, gate, job.dispatch.run_id, job.dispatch.project_id)
    budget(agents, ledger)
    worker = RunWorker(get_settings(), Repository(), None, session_factory=NoSession)  # type: ignore[arg-type]
    before_step = worker._downgrade_watch(job, agents)
    assert before_step is not None
    before_step("research")
    return agents, cheap


async def test_an_exhausted_budget_still_stops_calls_after_a_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=1_000, max_cost_usd=1.0), max_output_tokens=100)
    ledger.spent_cost = 1.0
    agents, cheap = _downgraded(monkeypatch, Gate(), ledger)
    with pytest.raises(GatewayError, match="budget_exhausted:cost"):
        await agents.researcher.gateway.complete("m", [{"role": "user", "content": "hi"}])
    assert cheap.calls == 0


async def test_a_deny_policy_still_blocks_calls_after_a_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=100)
    agents, cheap = _downgraded(monkeypatch, Gate(deny=True), ledger)
    with pytest.raises(PolicyRefused):
        await agents.chief.gateway.complete("m", [{"role": "user", "content": "hi"}])
    assert cheap.calls == 0


async def test_a_downgraded_role_reaches_the_cheaper_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=100)
    agents, cheap = _downgraded(monkeypatch, Gate(), ledger)
    with pytest.raises(Reached):
        await agents.planner.gateway.complete("m", [{"role": "user", "content": "hi"}])
    assert cheap.calls == 1


def test_a_downgraded_role_keeps_its_backup_when_the_cheaper_model_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=100)

    agents, cheap = _downgraded(monkeypatch, Gate(), ledger, backed=True)

    model = innermost(agents.chief)
    assert isinstance(model, FailoverCompleter)
    assert model.inner is cheap
    assert (model.backup_key, model.backup_alias) == ("c-old-backup", "old")


def test_the_backup_the_owner_chose_for_the_cheaper_model_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=100)

    agents, cheap = _downgraded(
        monkeypatch, Gate(), ledger, backed=True, plans={"{cheap}": {"backup": "c-new-backup"}}
    )

    model = innermost(agents.researcher)
    assert isinstance(model, FailoverCompleter)
    assert model.inner is cheap
    assert (model.backup_key, model.backup_alias) == ("c-new-backup", "connection:c-new-backup")


def test_a_backup_still_naming_the_retired_gateway_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=100)

    agents, cheap = _downgraded(
        monkeypatch, Gate(), ledger, backed=True, plans={"{cheap}": {"backup": GATEWAY}}
    )

    model = innermost(agents.researcher)
    assert isinstance(model, FailoverCompleter)
    assert model.backup_key == "c-old-backup", "the role keeps the backup it had"


def test_a_role_without_any_backup_gets_the_cheaper_model_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=100)

    agents, cheap = _downgraded(monkeypatch, Gate(), ledger)

    assert innermost(agents.critic) is cheap
