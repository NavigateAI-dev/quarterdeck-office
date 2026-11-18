"""N12: a paid tool call is checked before it is sent and counted after."""

from typing import Any
from uuid import uuid4

import pytest

from app.budget import Budget, RunLedger
from app.events import InvocationEvent
from app.gateway.client import GatewayError
from app.mcp import review as mcp_review
from app.mcp.calling import McpTools, Reachable, ToolRefused
from app.mcp.client import McpError, ToolResult
from app.mcp.metering import ToolMeter


def tool(name: str = "scrape", description: str = "Scrape a page.") -> dict[str, Any]:
    return {"name": name, "description": description, "schema": {}, "claims": {}}


class Gate:
    def enforce(self, hook: Any, name: str, **facts: Any) -> None:
        return None


class Transport:
    def __init__(self, result: ToolResult | Exception | None = None) -> None:
        self.result = result or ToolResult(content="page text", failed=False)
        self.calls: list[str] = []

    async def call(
        self, url: str, token: str | None, allow: list[str], name: str, arguments: dict
    ) -> ToolResult:
        self.calls.append(name)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def ledger(max_cost: float | None = None, spent: float = 0.0) -> RunLedger:
    return RunLedger(Budget(max_tokens=100_000, max_cost_usd=max_cost), 1000, spent_cost=spent)


def setup(
    price: float | None,
    run_ledger: RunLedger | None = None,
    refusal: str | None = None,
    transport: Transport | None = None,
) -> tuple[McpTools, list[InvocationEvent], list[tuple[str, dict]], Transport]:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex", price_usd=price)
    usage: list[InvocationEvent] = []
    notes: list[tuple[str, dict]] = []
    sent = transport or Transport()
    meter = ToolMeter(
        run_ledger if run_ledger is not None else ledger(),
        usage.append,
        lambda: refusal,
        run_id=uuid4(),
        task_id=uuid4(),
        project_id=uuid4(),
    )
    tools = McpTools(
        servers=[Reachable(uuid4(), "Apify", "https://mcp.apify.com", "t", [tool()], review)],
        gate=Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=sent,  # type: ignore[arg-type]
        record=lambda kind, data: notes.append((kind, data)),
        meter=meter,
    )
    return tools, usage, notes, sent


async def test_a_priced_call_is_counted_at_its_price() -> None:
    run_ledger = ledger()
    tools, usage, _, _ = setup(0.4, run_ledger)
    assert await tools.call("Apify", "scrape", {"url": "x"}) == "page text"
    [event] = usage
    assert event.estimated_cost == 0.4
    assert event.role == "tools" and event.provider == "mcp"
    assert event.model_alias == "Apify:scrape"
    assert run_ledger.spent_cost == pytest.approx(0.4)


async def test_an_unpriced_call_records_no_spending() -> None:
    run_ledger = ledger()
    tools, usage, _, _ = setup(None, run_ledger)
    await tools.call("Apify", "scrape", {})
    assert usage == [] and run_ledger.spent_cost == 0


async def test_a_call_that_would_pass_the_cost_ceiling_stops_the_task_unsent() -> None:
    """The B1 question: the task pauses and the owner may raise the ceiling."""
    tools, usage, _, sent = setup(0.5, ledger(max_cost=1.0, spent=0.7))
    with pytest.raises(GatewayError) as stopped:
        await tools.call("Apify", "scrape", {})
    assert stopped.value.code == "budget_exhausted:cost"
    assert sent.calls == [] and usage == []


async def test_a_call_that_fits_under_the_ceiling_is_sent() -> None:
    tools, usage, _, sent = setup(0.3, ledger(max_cost=1.0, spent=0.7))
    await tools.call("Apify", "scrape", {})
    assert sent.calls == ["scrape"] and len(usage) == 1


async def test_past_a_monthly_limit_the_call_is_refused_and_the_task_goes_on() -> None:
    tools, usage, notes, sent = setup(0.1, refusal="monthly_limit_reached")
    with pytest.raises(ToolRefused) as refused:
        await tools.call("Apify", "scrape", {})
    assert refused.value.reason == "monthly_limit_reached"
    assert sent.calls == [] and usage == []
    assert notes == [
        (
            "mcp_call",
            {
                "server": "Apify",
                "tool": "scrape",
                "outcome": "monthly_limit_reached",
                "characters": 0,
            },
        )
    ]


async def test_an_unpriced_call_is_not_held_by_a_monthly_limit() -> None:
    """The limit bounds spending; a call that spends nothing is not spending."""
    tools, _, _, sent = setup(None, refusal="monthly_limit_reached")
    await tools.call("Apify", "scrape", {})
    assert sent.calls == ["scrape"]


async def test_a_call_that_was_sent_is_charged_whatever_came_back() -> None:
    """The office cannot see whether the vendor billed a failure; free would understate."""
    for result in (ToolResult(content="", failed=True), McpError("unreachable")):
        tools, usage, _, _ = setup(0.2, transport=Transport(result))
        await tools.call("Apify", "scrape", {})
        assert [event.estimated_cost for event in usage] == [0.2], result


async def test_a_call_refused_before_it_left_is_not_charged() -> None:
    tools, usage, _, _ = setup(0.2, transport=Transport(McpError("host_not_allowed")))
    await tools.call("Apify", "scrape", {})
    assert usage == []


def test_the_agent_choosing_calls_is_told_the_price() -> None:
    tools, _, _, _ = setup(0.25)
    [offered] = tools.offered()
    assert offered["price_usd"] == 0.25
