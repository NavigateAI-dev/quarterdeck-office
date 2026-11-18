"""PC1: a call to a failing provider goes to the backup the owner chose."""

from typing import Any

import pytest

from app.failover import GATEWAY, Breaker, FailoverCompleter, reason_for
from app.gateway.client import GatewayError


class Model:
    """A completer that answers, or fails with the codes it is given in turn."""

    def __init__(self, name: str, failures: list[str] | None = None) -> None:
        self.name = name
        self.failures = list(failures or [])
        self.calls: list[str] = []

    async def complete(self, model_alias: str, messages: list, *args: Any, **kwargs: Any) -> str:
        self.calls.append(model_alias)
        if self.failures:
            raise GatewayError(self.failures.pop(0))
        return f"{self.name} answered"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def failover(primary: Model, backup: Model, breaker: Breaker, come_back: bool = True) -> tuple:
    moved: list[str] = []
    wrapped = FailoverCompleter(
        primary,
        key="deepseek-id",
        backup_key=GATEWAY,
        make_backup=lambda: backup,
        backup_alias="researcher",
        come_back=come_back,
        on_move=moved.append,
        breaker=breaker,
    )
    return wrapped, moved


MESSAGES = [{"role": "user", "content": "hi"}]


def test_what_moves_and_what_never_does() -> None:
    assert reason_for("insufficient_balance") == "no_credit"
    assert reason_for("credential_invalid") == "key_refused"
    assert reason_for("provider_unavailable") == "failing"
    assert reason_for("rate_limited") == "failing"
    # The task's own ceiling, a refusal and a bad answer are not the provider failing.
    for code in (
        "budget_exhausted:cost",
        "model_refusal",
        "malformed_response",
        "request_rejected",
    ):
        assert reason_for(code) is None, code


async def test_out_of_credit_moves_at_once_to_the_backup() -> None:
    primary, backup = Model("deepseek", ["insufficient_balance"]), Model("gateway")
    wrapped, moved = failover(primary, backup, Breaker(Clock()))
    assert await wrapped.complete("connection:deepseek-id", MESSAGES) == "gateway answered"
    assert backup.calls == ["researcher"], "the backup is asked under its own alias"
    assert moved == ["no_credit"]


async def test_one_outage_is_not_enough_three_in_a_row_are() -> None:
    breaker = Breaker(Clock())
    primary = Model("deepseek", ["provider_unavailable", "provider_unavailable"])
    backup = Model("gateway")
    wrapped, moved = failover(primary, backup, breaker)
    for _ in range(2):
        with pytest.raises(GatewayError):
            await wrapped.complete("connection:deepseek-id", MESSAGES)
    assert backup.calls == [] and moved == []
    primary.failures = ["provider_unavailable"]
    assert await wrapped.complete("connection:deepseek-id", MESSAGES) == "gateway answered"
    assert moved == ["failing"]


async def test_a_success_between_failures_starts_the_count_again() -> None:
    breaker = Breaker(Clock())
    primary = Model("deepseek", ["provider_unavailable", "provider_unavailable"])
    wrapped, _ = failover(primary, Model("gateway"), breaker)
    for _ in range(2):
        with pytest.raises(GatewayError):
            await wrapped.complete("x", MESSAGES)
    assert await wrapped.complete("x", MESSAGES) == "deepseek answered"
    primary.failures = ["provider_unavailable"]
    with pytest.raises(GatewayError):
        await wrapped.complete("x", MESSAGES)


async def test_the_tasks_own_budget_never_moves_it() -> None:
    primary, backup = Model("deepseek", ["budget_exhausted:cost"]), Model("gateway")
    wrapped, moved = failover(primary, backup, Breaker(Clock()))
    with pytest.raises(GatewayError, match="budget_exhausted"):
        await wrapped.complete("x", MESSAGES)
    assert backup.calls == [] and moved == []


async def test_once_moved_other_work_goes_straight_to_the_backup() -> None:
    breaker = Breaker(Clock())
    primary, backup = Model("deepseek", ["insufficient_balance"]), Model("gateway")
    first, _ = failover(primary, backup, breaker)
    await first.complete("x", MESSAGES)
    second, moved = failover(primary, backup, breaker)
    assert await second.complete("x", MESSAGES) == "gateway answered"
    assert primary.calls == ["x"], "the failing provider is not asked again at once"
    assert moved == ["no_credit"], "each task says it moved"


async def test_the_primary_is_tried_again_after_five_minutes() -> None:
    clock = Clock()
    breaker = Breaker(clock)
    primary, backup = Model("deepseek", ["insufficient_balance"]), Model("gateway")
    wrapped, _ = failover(primary, backup, breaker)
    await wrapped.complete("x", MESSAGES)
    clock.now += 301
    assert await wrapped.complete("x", MESSAGES) == "deepseek answered"
    assert breaker.view("deepseek-id") is None, "it is back in use"


async def test_without_come_back_the_backup_stays() -> None:
    clock = Clock()
    breaker = Breaker(clock)
    primary, backup = Model("deepseek", ["insufficient_balance"]), Model("gateway")
    wrapped, _ = failover(primary, backup, breaker, come_back=False)
    await wrapped.complete("x", MESSAGES)
    clock.now += 3600
    assert await wrapped.complete("x", MESSAGES) == "gateway answered"
    assert primary.calls == ["x"]


async def test_a_failing_backup_says_so_rather_than_trying_further() -> None:
    primary = Model("deepseek", ["insufficient_balance"])
    backup = Model("gateway", ["provider_unavailable"])
    wrapped, _ = failover(primary, backup, Breaker(Clock()))
    with pytest.raises(GatewayError, match="provider_unavailable"):
        await wrapped.complete("x", MESSAGES)
