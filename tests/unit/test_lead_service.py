"""The lead's front door: one request in, one checked answer out."""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.gateway.client import Completion, ToolCall, Usage
from app.lead.prompt import Section
from app.lead.service import LeadRequest, answer
from app.tools.pipeline import ToolResult, ToolSpec

NOW = datetime(2026, 10, 16, 14, 52, tzinfo=UTC)


def said(text: str) -> Completion:
    return Completion(model_alias="m", model="m", content=text, usage=Usage(), latency_seconds=0.0)


def call(name: str, arguments: dict[str, Any]) -> Completion:
    return Completion(
        model_alias="m",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[
            ToolCall(id="c1", name=name, arguments=arguments, raw_arguments=json.dumps(arguments))
        ],
    )


class Model:
    def __init__(self, *turns: Completion) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []

    async def complete(
        self, model_alias: str, messages: list[dict[str, Any]], **options: Any
    ) -> Any:
        self.seen.append(list(messages))
        return self.turns.pop(0)


class Query(BaseModel):
    query: str


async def knmi(arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(text="- KNMI (https://www.knmi.nl/weer): rain at 15:00")


SEARCH = ToolSpec("search", "A web search.", knmi, Query, concurrency_safe=True)


def request(model: Model, **options: Any) -> LeadRequest:
    return LeadRequest(
        request="Rain in Lyon today?",
        completer_for=lambda role: (model, role),
        work=[SEARCH],
        now=NOW,
        **options,
    )


async def test_a_simple_question_is_one_call_and_comes_back_answered() -> None:
    model = Model(said("No rain today."))

    reply = await answer(request(model))

    assert reply.text == "No rain today."
    assert reply.stopped == "answered"
    assert reply.calls == 1
    assert reply.citations.ok


async def test_the_lead_gets_the_prompt_in_cache_order_with_its_context() -> None:
    model = Model(said("ok"))

    await answer(request(model, doctrine="Cite sources.", sections=[Section("Project", "Pilot")]))

    system, context, asked = model.seen[0]
    assert "Cite sources." in system["content"]
    assert "## Project\nPilot" in context["content"]
    assert asked == {"role": "user", "content": "Rain in Lyon today?"}


async def test_an_answer_citing_a_page_no_tool_returned_is_sent_back_once_to_fix() -> None:
    model = Model(
        call("search", {"query": "rain Lyon"}),
        said("Rain at 15:00 (https://www.knmi.nl/weer, https://made-up.example)."),
        said("Rain at 15:00 (https://www.knmi.nl/weer)."),
    )

    reply = await answer(request(model))

    assert reply.text == "Rain at 15:00 (https://www.knmi.nl/weer)."
    assert reply.citations.ok
    correction = model.seen[2][-1]["content"]
    assert "https://made-up.example" in correction


async def test_a_citation_still_unresolved_after_the_fix_is_flagged_not_hidden() -> None:
    model = Model(said("See https://made-up.example."), said("See https://made-up.example."))

    reply = await answer(request(model))

    assert not reply.citations.ok
    assert reply.citations.unresolved == ["https://made-up.example"]


async def test_the_lead_offers_its_own_tools_beside_the_work_tools() -> None:
    offered: list[str] = []

    class Seeing(Model):
        async def complete(
            self, model_alias: str, messages: list[dict[str, Any]], **options: Any
        ) -> Any:
            offered.extend(tool["name"] for tool in options.get("tools") or [])
            return await super().complete(model_alias, messages)

    await answer(request(Seeing(said("ok"))))

    assert set(offered) == {"search", "read_more", "todo", "delegate", "review", "propose_team_run"}


async def test_a_proposed_team_run_comes_back_for_the_owner() -> None:
    model = Model(
        call(
            "propose_team_run", {"objective": "Full NL e-bike market study", "why": "Many sources"}
        ),
        said("Here is a first view; I proposed a full team run."),
    )

    reply = await answer(request(model))

    assert reply.proposals == [{"objective": "Full NL e-bike market study", "why": "Many sources"}]


async def test_the_lead_runs_on_the_chief_role_model() -> None:
    roles: list[str] = []
    model = Model(said("ok"))

    def completer_for(role: str) -> tuple[Any, str]:
        roles.append(role)
        return model, role

    await answer(LeadRequest(request="Q", completer_for=completer_for, work=[], now=NOW))

    assert roles[0] == "chief"
