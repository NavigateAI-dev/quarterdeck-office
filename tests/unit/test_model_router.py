"""The model router: no stronger model than a task needs.

Learned from LangChain's Open SWE router: one decision per task from its
request, a Jev choice over tiers described in plain words, accepted only when
confident. Here a tier is a ceiling: hard work keeps the office's setup, easier
work moves only the roles whose model costs much more than the tier's.
"""

import asyncio
from typing import Any

import pytest

from app.model_router import (
    MIN_CONFIDENCE,
    RouteDecision,
    Target,
    decide,
    moves,
    price_of,
    targets,
)

CATALOGUE = {
    "claude-haiku-5-5": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
    "gpt-5.6-luna": {"input_cost_per_token": 2e-7, "output_cost_per_token": 1.2e-6},
    "deepseek-flash": {"input_cost_per_token": 3e-7, "output_cost_per_token": 1.2e-6},
}


def connection(
    name: str, model: str, provider: str = "openai", status: str = "MODEL_AVAILABLE"
) -> dict[str, Any]:
    return {
        "connection_id": name,
        "provider": provider,
        "model_id": model,
        "base_url": None,
        "status": status,
    }


HAIKU = connection("c-haiku", "claude-haiku-5-5", "anthropic")
LUNA = connection("c-luna", "gpt-5.6-luna")
FLASH = connection("c-flash", "deepseek-flash", "deepseek")


def test_the_cheapest_connection_is_fast_and_the_middle_one_balanced() -> None:
    pool = targets([FLASH, LUNA, HAIKU], CATALOGUE)

    assert {tier: t.entry["connection_id"] for tier, t in pool.items()} == {
        "fast": "c-haiku",
        "balanced": "c-luna",
    }
    assert pool["fast"].price == pytest.approx(0.6)


def test_one_connection_is_a_fast_tier_only() -> None:
    assert set(targets([HAIKU], CATALOGUE)) == {"fast"}


def test_untested_unpriced_and_local_models_are_not_targets() -> None:
    pool = targets(
        [
            connection("c-broken", "claude-haiku-5-5", status="FAILED"),
            connection("c-unknown", "mystery-model"),
            connection("c-local", "claude-haiku-5-5", provider="ollama"),
        ],
        CATALOGUE,
    )

    assert pool == {}


def test_a_target_is_shaped_like_an_owner_chosen_role_model() -> None:
    assert targets([HAIKU], CATALOGUE)["fast"].entry == {
        "connection_id": "c-haiku",
        "provider": "anthropic",
        "model_id": "claude-haiku-5-5",
        "base_url": None,
    }


def test_prices_count_input_and_output_per_million() -> None:
    assert price_of(CATALOGUE, "openai", "gpt-5.6-luna") == pytest.approx(1.4)
    assert price_of(CATALOGUE, "ollama", "gpt-5.6-luna") is None
    assert price_of(CATALOGUE, "openai", "unknown") is None


class Judge:
    def __init__(self, answer: Any, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.asked: list[dict[str, Any]] = []

    async def model_route(self, task: str, instructions: str, criteria: dict[str, str]) -> Any:
        self.asked.append({"task": task, "criteria": criteria})
        await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


POOL = {"fast": Target(HAIKU, 0.6), "balanced": Target(LUNA, 1.4)}


async def test_a_confident_choice_routes_the_task() -> None:
    judge = Judge(("fast", 0.91))

    decision = await decide("What is the VAT rate in NL?", POOL, judge)

    assert decision == RouteDecision("fast", 0.91, "routed", POOL["fast"])
    assert set(judge.asked[0]["criteria"]) == {"fast", "balanced", "performance"}


async def test_performance_keeps_the_office_setup() -> None:
    decision = await decide("Q", POOL, Judge(("performance", 0.8)))

    assert (decision.tier, decision.outcome, decision.target) == ("performance", "routed", None)


async def test_only_the_tiers_the_office_has_are_offered() -> None:
    judge = Judge(("fast", 0.8))

    await decide("Q", {"fast": POOL["fast"]}, judge)

    assert set(judge.asked[0]["criteria"]) == {"fast", "performance"}


async def test_an_unsure_choice_keeps_the_office_setup() -> None:
    decision = await decide("Q", POOL, Judge(("fast", MIN_CONFIDENCE - 0.01)))

    assert (decision.tier, decision.outcome, decision.target) == (None, "low_confidence", None)


async def test_a_choice_outside_the_pool_keeps_the_office_setup() -> None:
    decision = await decide("Q", {"fast": POOL["fast"]}, Judge(("balanced", 0.99)))

    assert (decision.tier, decision.outcome) == (None, "no_choice")


@pytest.mark.parametrize("judge", [Judge(RuntimeError("down")), Judge(("fast", 0.9), delay=1.0)])
async def test_a_failed_or_slow_router_keeps_the_office_setup(judge: Judge) -> None:
    decision = await decide("Q", POOL, judge, timeout_s=0.05)

    assert (decision.tier, decision.outcome) == (None, "unavailable")


async def test_no_judge_or_no_pool_means_no_routing() -> None:
    assert (await decide("Q", POOL, None)).outcome == "unavailable"
    assert (await decide("Q", {}, Judge(("fast", 0.9)))).outcome == "nothing_to_route"


async def test_the_request_is_cut_to_its_last_8000_characters() -> None:
    judge = Judge(("fast", 0.9))

    await decide("x" * 9000 + "the ask", POOL, judge)

    assert len(judge.asked[0]["task"]) == 8000
    assert judge.asked[0]["task"].endswith("the ask")


# The office today: a strong Chief, cheap researcher, strategist and critic.
CURRENT = {"chief": 24.0, "researcher": 1.45, "strategist": 1.5, "critic": 1.4}


def test_balanced_moves_only_the_roles_on_a_much_dearer_model() -> None:
    decision = RouteDecision("balanced", 0.9, "routed", POOL["balanced"])

    assert set(moves(decision, CURRENT, pinned=set())) == {"chief"}


def test_fast_moves_every_role_that_costs_twice_the_fast_model() -> None:
    decision = RouteDecision("fast", 0.9, "routed", POOL["fast"])

    moved = moves(decision, CURRENT | {"summarist": 1.0}, pinned=set())

    assert set(moved) == {"chief", "researcher", "strategist", "critic"}
    assert moved["chief"]["connection_id"] == "c-haiku"


def test_pinned_and_unpriced_roles_are_never_moved() -> None:
    decision = RouteDecision("fast", 0.9, "routed", POOL["fast"])

    moved = moves(decision, CURRENT | {"advisor": None}, pinned={"chief"})

    assert "chief" not in moved and "advisor" not in moved


def test_no_target_moves_nothing() -> None:
    assert moves(RouteDecision("performance", 0.9, "routed"), CURRENT, set()) == {}
    assert moves(RouteDecision(None, None, "unavailable"), CURRENT, set()) == {}
