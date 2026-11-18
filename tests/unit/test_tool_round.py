"""tool_round.gather: today's one-shot tool choice (characterisation).

A bounded per-step loop is meant to replace this. These tests
pin what the replacement must keep (only offered tools are called, duplicates
are dropped, a refusal costs only that tool, an owner question stops the work)
and what it is meant to change (one choice, results cut at RESULT_CHARS).
"""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.agents.tool_round import RESULT_CHARS, gather, tool_address
from app.context import ProjectContext
from app.gateway.client import GatewayError
from app.mcp.calling import ToolRefused
from app.mcp.client import McpError
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk

CONTEXT = ProjectContext(organization="Office", project_id=uuid4(), project_name="Pilot")
OFFERED = [
    {"server": "crm", "name": "search", "description": "Find contacts", "schema": {}},
    {"server": "web", "name": "fetch", "description": "Read a page", "schema": {}},
]


class Chooser:
    """A model that answers the tool-choice prompt with a fixed reply."""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, model_alias: str, messages: list[dict[str, str]]) -> Any:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=self.reply)


class Tools:
    def __init__(self, results: dict[tuple[str, str], str | Exception]) -> None:
        self.results = results
        self.made: list[tuple[str, str, dict[str, Any]]] = []

    def offered(self) -> list[dict[str, Any]]:
        return OFFERED

    async def call(self, server: str, name: str, arguments: dict[str, Any]) -> str:
        self.made.append((server, name, arguments))
        result = self.results[(server, name)]
        if isinstance(result, Exception):
            raise result
        return result


def agent(reply: str | Exception) -> SimpleNamespace:
    return SimpleNamespace(
        gateway=Chooser(reply), model_alias="research", _emit=lambda *a, **k: None
    )


def choice(*calls: dict[str, Any]) -> str:
    return json.dumps({"calls": list(calls)})


async def test_calls_only_what_was_offered_and_drops_duplicates() -> None:
    tools = Tools({("crm", "search"): "3 contacts"})
    reply = choice(
        {"server": "crm", "tool": "search", "arguments": {"q": "acme"}},
        {"server": "crm", "tool": "search", "arguments": {"q": "acme"}},
        {"server": "bank", "tool": "pay", "arguments": {}},
    )

    evidence = await gather(agent(reply), tools, "Who?", CONTEXT, purpose="to answer", limit=5)  # type: ignore[arg-type]

    assert tools.made == [("crm", "search", {"q": "acme"})]
    assert [e.address for e in evidence] == [tool_address("crm", "search", {"q": "acme"})]


async def test_one_choice_only_and_held_to_the_limit() -> None:
    tools = Tools({("crm", "search"): "a", ("web", "fetch"): "b"})
    model = agent(
        choice(
            {"server": "crm", "tool": "search", "arguments": {}},
            {"server": "web", "tool": "fetch", "arguments": {}},
        )
    )

    evidence = await gather(model, tools, "Q", CONTEXT, purpose="to answer", limit=1)  # type: ignore[arg-type]

    assert model.gateway.calls == 1
    assert len(evidence) == 1


async def test_results_are_cut_at_the_result_limit() -> None:
    tools = Tools({("web", "fetch"): "x" * (RESULT_CHARS + 500)})
    reply = choice({"server": "web", "tool": "fetch", "arguments": {}})

    evidence = await gather(agent(reply), tools, "Q", CONTEXT, purpose="to answer", limit=2)  # type: ignore[arg-type]

    assert len(evidence[0].text) == RESULT_CHARS


async def test_a_refused_call_costs_only_that_tool() -> None:
    tools = Tools({("crm", "search"): ToolRefused("blocked"), ("web", "fetch"): "page"})
    reply = choice(
        {"server": "crm", "tool": "search", "arguments": {}},
        {"server": "web", "tool": "fetch", "arguments": {}},
    )

    evidence = await gather(agent(reply), tools, "Q", CONTEXT, purpose="to answer", limit=2)  # type: ignore[arg-type]

    assert [e.label for e in evidence] == ["web · fetch"]


async def test_a_broken_tool_server_costs_only_that_tool() -> None:
    tools = Tools({("crm", "search"): McpError("unreachable"), ("web", "fetch"): "page"})
    reply = choice(
        {"server": "crm", "tool": "search", "arguments": {}},
        {"server": "web", "tool": "fetch", "arguments": {}},
    )

    evidence = await gather(agent(reply), tools, "Q", CONTEXT, purpose="to answer", limit=2)  # type: ignore[arg-type]

    assert [e.label for e in evidence] == ["web · fetch"]


async def test_an_owner_question_stops_the_work() -> None:
    tools = Tools({("crm", "search"): PolicyAsk(Decision(Verdict.ASK, "ask the owner", "crm"), 1)})
    reply = choice({"server": "crm", "tool": "search", "arguments": {}})

    with pytest.raises(PolicyAsk):
        await gather(agent(reply), tools, "Q", CONTEXT, purpose="to answer", limit=2)  # type: ignore[arg-type]


@pytest.mark.parametrize("reply", ["not json", GatewayError("provider_unavailable")])
async def test_a_bad_or_failed_choice_means_no_tools(reply: str | Exception) -> None:
    tools = Tools({})

    evidence = await gather(agent(reply), tools, "Q", CONTEXT, purpose="to answer", limit=2)  # type: ignore[arg-type]

    assert evidence == []
    assert tools.made == []


async def test_no_tools_no_model_call() -> None:
    model = agent(choice())

    assert await gather(model, None, "Q", CONTEXT, purpose="to answer", limit=2) == []  # type: ignore[arg-type]
    assert model.gateway.calls == 0
