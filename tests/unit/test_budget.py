"""Run budgets: reservations before every model call, reconciled afterwards."""

from typing import Any

import pytest

from app.budget import Budget, BudgetedCompleter, BudgetExhausted, RunLedger
from app.gateway.client import Completion, GatewayError, Usage

MESSAGES = [{"role": "user", "content": "x" * 400}]  # ~100 tokens estimated


class Inner:
    def __init__(self, prompt: int | None = 120, completion: int | None = 30, cost: Any = 0.01):
        self.calls = 0
        self.usage = Usage(prompt_tokens=prompt, completion_tokens=completion)
        self.cost = cost
        self.error: GatewayError | None = None

    async def complete(
        self, alias: str, messages: list[dict[str, str]], schema: Any = None
    ) -> Completion:
        self.calls += 1
        if self.error:
            raise self.error
        return Completion(
            model_alias=alias,
            model="m",
            content="{}",
            usage=self.usage,
            latency_seconds=0.1,
            estimated_cost=self.cost,
        )


def ledger(**budget: Any) -> RunLedger:
    return RunLedger(Budget(**({"max_tokens": 10_000} | budget)), max_output_tokens=500)


async def test_actual_usage_replaces_the_reservation() -> None:
    books = ledger()
    await BudgetedCompleter(Inner(), books).complete("a", MESSAGES)
    assert books.spent_tokens == 150
    assert books.reserved_tokens == 0
    assert books.spent_cost == pytest.approx(0.01)


async def test_a_call_that_would_exceed_the_token_budget_is_never_made() -> None:
    inner = Inner()
    books = ledger(max_tokens=550)  # reservation is ~100 prompt + 500 output
    with pytest.raises(GatewayError, match="budget_exhausted"):
        await BudgetedCompleter(inner, books).complete("a", MESSAGES)
    assert inner.calls == 0


async def test_the_cost_budget_stops_further_calls() -> None:
    books = ledger(max_cost_usd=0.015)
    completer = BudgetedCompleter(Inner(cost=0.01), books)
    await completer.complete("a", MESSAGES)
    await completer.complete("a", MESSAGES)  # 0.01 spent < 0.015: allowed
    with pytest.raises(GatewayError, match="budget_exhausted"):
        await completer.complete("a", MESSAGES)  # 0.02 spent: stop
    assert books.exhausted == "cost"


async def test_the_time_budget_stops_further_calls() -> None:
    clock = [0.0]
    books = RunLedger(Budget(max_tokens=10_000, max_minutes=1), 500, clock=lambda: clock[0])
    completer = BudgetedCompleter(Inner(), books)
    await completer.complete("a", MESSAGES)
    clock[0] = 61.0
    with pytest.raises(GatewayError, match="budget_exhausted"):
        await completer.complete("a", MESSAGES)
    assert books.exhausted == "time"


async def test_a_failed_call_releases_its_reservation() -> None:
    inner = Inner()
    inner.error = GatewayError("provider_timeout")
    books = ledger()
    with pytest.raises(GatewayError, match="provider_timeout"):
        await BudgetedCompleter(inner, books).complete("a", MESSAGES)
    assert books.reserved_tokens == 0
    assert books.spent_tokens == 0


async def test_unreported_usage_is_counted_conservatively_not_as_zero() -> None:
    books = ledger()
    await BudgetedCompleter(Inner(prompt=None, completion=None, cost=None), books).complete(
        "a", MESSAGES
    )
    assert books.spent_tokens == 601  # the whole reservation: 101 estimated + 500
    assert books.cost_reported is False


def test_prior_spend_carries_over_on_resume() -> None:
    books = RunLedger(Budget(max_tokens=1000), 500, spent_tokens=900)
    with pytest.raises(BudgetExhausted):
        books.reserve(MESSAGES)


def test_budget_values_are_validated() -> None:
    with pytest.raises(ValueError):
        Budget(max_tokens=0)


async def test_every_role_is_routed_through_the_ledger() -> None:
    from app.agent_routing import budget
    from app.runtime.worker import build_agents
    from tests.unit.test_direct_completion import settings

    agents = build_agents(Inner(), settings())  # type: ignore[arg-type]
    books = RunLedger(Budget(max_tokens=100), 500)
    budget(agents, books)
    with pytest.raises(GatewayError, match="budget_exhausted"):
        await agents.critic.gateway.complete("a", MESSAGES)


async def test_an_exhausted_budget_fails_the_agent_with_its_reason() -> None:
    import json

    from app.agents import AgentOutputError, ChiefAgent
    from tests.unit.test_agents import chief_payload, config, context

    class Content(Inner):
        async def complete(
            self, alias: str, messages: list[dict[str, str]], schema: Any = None
        ) -> Completion:
            done = await super().complete(alias, messages)
            return done.model_copy(update={"content": json.dumps(chief_payload())})

    books = RunLedger(Budget(max_tokens=10), 500)
    agent = ChiefAgent(BudgetedCompleter(Content(), books), config())  # type: ignore[arg-type]
    with pytest.raises(AgentOutputError, match="budget_exhausted"):
        await agent.run("objective", context())


def test_a_blank_cost_or_time_limit_means_no_limit() -> None:
    from app.config import Settings

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://office:secret@localhost/office",
        run_max_cost_usd="",
        run_max_minutes="",
    )
    assert settings.run_max_cost_usd is None
    assert settings.run_max_minutes is None


async def test_a_truncated_response_is_counted_at_its_reservation_not_free() -> None:
    """The provider bills a response cut off at the cap; the ledger must too."""
    inner = Inner()
    inner.error = GatewayError("incomplete_response")
    books = ledger()
    with pytest.raises(GatewayError, match="incomplete_response"):
        await BudgetedCompleter(inner, books).complete("a", MESSAGES)
    assert books.spent_tokens == 601
    assert books.reserved_tokens == 0
    assert books.cost_reported is False


async def test_a_larger_output_cap_is_reserved_for_the_call_that_asks_for_it() -> None:
    books = RunLedger(Budget(max_tokens=1_000), 500)
    with pytest.raises(GatewayError, match="budget_exhausted"):
        await BudgetedCompleter(Inner(), books).complete("a", MESSAGES, max_tokens=2_000)


async def test_the_output_cap_reaches_the_inner_completer() -> None:
    seen: list[Any] = []

    class Recording(Inner):
        async def complete(
            self,
            alias: str,
            messages: list[dict[str, str]],
            max_tokens: int | None = None,
            schema: Any = None,
        ) -> Completion:
            seen.append(max_tokens)
            return await super().complete(alias, messages)

    books = ledger(max_tokens=100_000)
    await BudgetedCompleter(Recording(), books).complete("a", MESSAGES, max_tokens=16_000)
    assert seen == [16_000]


def test_a_limit_raised_while_the_task_runs_is_used_before_refusing() -> None:
    """Found in the team trial: the owner raised a running task's limit, and the
    task still stopped at the old one, held from when it started."""
    raised = Budget(max_tokens=5000)
    books = RunLedger(Budget(max_tokens=1000), 500, spent_tokens=900, refresh=lambda: raised)
    books.reserve([{"role": "user", "content": "x" * 40}])
    assert books.budget.max_tokens == 5000


def test_a_limit_that_was_not_raised_still_refuses() -> None:
    books = RunLedger(Budget(max_tokens=1000), 500, spent_tokens=900, refresh=lambda: None)
    with pytest.raises(BudgetExhausted):
        books.reserve([{"role": "user", "content": "x" * 40}])


def test_finishing_gives_room_for_one_writing_pass_at_the_limit_reached() -> None:
    from app.budget import room_to_finish

    raised = room_to_finish(
        {"max_tokens": 400_000, "max_minutes": 30},
        {"tokens": 397_284, "cost_usd": 0.11},
        "x" * 40_000,
        8_000,
    )
    assert raised["max_tokens"] == 397_284 + 2 * (10_000 + 4_000 + 8_000)
    assert raised["max_minutes"] == 30


def test_finishing_leaves_a_limit_with_room_to_spare_alone() -> None:
    from app.budget import room_to_finish

    budget = {"max_tokens": 900_000, "max_cost_usd": 5.0}
    assert room_to_finish(budget, {"tokens": 100_000, "cost_usd": 1.0}, "", 8_000) == budget
