"""The lead in conversations: the chat allowance and watch offers."""

from typing import Any

import pytest

from app.channels.lookup import watch_spec
from app.gateway.client import Completion, GatewayError, Usage
from app.lead.chat import AllowanceCompleter
from app.tools.pipeline import ToolPipeline


class Model:
    def __init__(self, prompt: int | None = 100, completion: int | None = 20) -> None:
        self.usage = Usage(prompt_tokens=prompt, completion_tokens=completion)
        self.given: list[dict[str, Any]] = []

    async def complete(self, model_alias: str, messages: Any, **options: Any) -> Completion:
        self.given.append(options)
        return Completion(
            model_alias=model_alias,
            model="m",
            content="ok",
            usage=self.usage,
            latency_seconds=0.0,
            estimated_cost=0.001,
        )


def allowance(model: Model, used: int, limit: int, recorded: list[Any]) -> AllowanceCompleter:
    return AllowanceCompleter(
        model,
        cap=500,
        allowance=limit,
        used_today=lambda: used,
        record=lambda tokens, cost: recorded.append((tokens, cost)),
    )


TURNS: list[dict[str, Any]] = [
    {"role": "user", "content": "Rain?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c", "function": {"name": "search"}}],
    },
    {"role": "tool", "tool_call_id": "c", "content": "rain at 15:00"},
]


async def test_each_call_is_recorded_against_the_allowance() -> None:
    recorded: list[Any] = []
    model = Model()

    await allowance(model, used=0, limit=10_000, recorded=recorded).complete(
        "chief", TURNS, tools=[{"name": "search"}]
    )

    assert recorded == [(120, 0.001)]
    assert model.given[0] == {"max_tokens": 500, "tools": [{"name": "search"}]}


async def test_a_call_past_the_allowance_is_not_made_and_stops_as_a_limit() -> None:
    model = Model()

    with pytest.raises(GatewayError, match="budget_exhausted:chat"):
        await allowance(model, used=9_900, limit=10_000, recorded=[]).complete("chief", TURNS)

    assert model.given == []


async def test_unknown_usage_is_charged_at_the_reservation() -> None:
    recorded: list[Any] = []

    await allowance(Model(prompt=None), used=0, limit=10_000, recorded=recorded).complete(
        "chief", TURNS
    )

    assert recorded[0][0] > 500


async def test_a_watch_is_offered_with_its_shape_checked() -> None:
    offered: list[dict[str, Any]] = []
    tools = ToolPipeline([watch_spec(offered)])

    outcome = await tools.call(
        "propose_watch",
        {
            "kind": "page",
            "target": "https://www.buienradar.nl/",
            "condition": "rain before 15:00",
            "every_minutes": 60,
            "title": "Rain",
        },
    )

    assert outcome.status == "ok" and "Start" in outcome.text
    assert offered == [
        {
            "kind": "page",
            "target": "https://www.buienradar.nl/",
            "condition": "rain before 15:00",
            "every_minutes": 60,
            "title": "Rain",
        }
    ]


async def test_a_watch_of_an_unknown_kind_is_refused() -> None:
    offered: list[dict[str, Any]] = []

    outcome = await ToolPipeline([watch_spec(offered)]).call(
        "propose_watch", {"kind": "telepathy", "target": "x"}
    )

    assert outcome.status == "refused"
    assert offered == []
