"""The lead's own tools: todo, delegate, review, propose a team run."""

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from app.channels.lookup import lookup_specs
from app.gateway.client import Completion, GatewayError, ToolCall, Usage
from app.lead.checks import check_citations
from app.lead.tools import BRIEF_STEPS, LeadDesk, SharedNotes, StepResult, lead_tools
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk
from app.tools.pipeline import ToolPipeline, ToolResult, ToolSpec


def said(text: str) -> Completion:
    return Completion(model_alias="m", model="m", content=text, usage=Usage(), latency_seconds=0.0)


def searching(query: str) -> Completion:
    return Completion(
        model_alias="m",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[
            ToolCall(
                id="c1",
                name="search",
                arguments={"query": query},
                raw_arguments=json.dumps({"query": query}),
            )
        ],
    )


class Model:
    def __init__(self, *turns: Completion | Exception, delay: float = 0.0) -> None:
        self.turns = list(turns)
        self.delay = delay
        self.seen: list[list[dict[str, Any]]] = []
        self.offered: list[list[str]] = []

    async def complete(
        self, model_alias: str, messages: list[dict[str, Any]], **options: Any
    ) -> Any:
        self.seen.append(list(messages))
        self.offered.append([tool["name"] for tool in options.get("tools") or []])
        await asyncio.sleep(self.delay)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class Query(BaseModel):
    query: str


async def found(arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(text=f"result for {arguments['query']} (https://knmi.nl)")


SEARCH = ToolSpec("search", "A web search.", found, Query, concurrency_safe=True)
RESULT = json.dumps(
    {
        "summary": "Rain from 15:00.",
        "findings": ["KNMI: rain 15:00"],
        "sources": ["https://knmi.nl"],
    }
)


def desk(models: dict[str, Model], **options: Any) -> LeadDesk:
    def completer_for(role: str) -> tuple[Any, str]:
        return models[role], role

    return LeadDesk(completer_for=completer_for, **options)


def pipeline(office: LeadDesk) -> ToolPipeline:
    return ToolPipeline(lead_tools([SEARCH], office))


async def test_todo_keeps_the_latest_list_and_says_skip_it_for_one_step_work() -> None:
    office = desk({})
    tools = pipeline(office)
    [spec] = [s for s in tools.specs() if s.name == "todo"]

    await tools.call("todo", {"items": [{"text": "Find sources", "status": "in_progress"}]})
    outcome = await tools.call(
        "todo",
        {
            "items": [
                {"text": "Find sources", "status": "done"},
                {"text": "Write", "status": "pending"},
            ]
        },
    )

    assert "skip it" in spec.description.lower()
    assert [item.status for item in office.todo] == ["done", "pending"]
    assert outcome.text == "Step list updated: 1 of 2 done."


async def test_delegate_runs_each_brief_as_its_own_researcher_and_returns_results_only() -> None:
    researcher = Model(searching("rain Lyon"), said(RESULT))
    office = desk({"researcher": researcher})

    outcome = await pipeline(office).call(
        "delegate", {"briefs": [{"brief": "Will it rain in Lyon today?", "role": "researcher"}]}
    )

    assert outcome.status == "ok"
    assert "Rain from 15:00." in outcome.text and "https://knmi.nl" in outcome.text
    assert "result for rain Lyon" not in outcome.text, "a result, not the transcript"
    assert researcher.seen[0][-1]["content"].endswith("Will it rain in Lyon today?")
    assert [r.summary for r in office.results] == ["Rain from 15:00."]


async def test_delegated_agents_have_the_work_tools_but_cannot_delegate() -> None:
    researcher = Model(said(RESULT))
    office = desk({"researcher": researcher})

    await pipeline(office).call("delegate", {"briefs": [{"brief": "b", "role": "researcher"}]})

    assert set(researcher.offered[0]) == {"search", "read_more"}


async def test_briefs_run_at_the_same_time() -> None:
    models = {"researcher": Model(said(RESULT), said(RESULT), delay=0.2)}
    office = desk(models)
    briefs = [{"brief": f"b{n}", "role": "researcher"} for n in range(2)]

    started = asyncio.get_running_loop().time()
    await pipeline(office).call("delegate", {"briefs": briefs})

    assert asyncio.get_running_loop().time() - started < 0.35


async def test_no_more_briefs_than_the_owner_limit_at_once() -> None:
    office = desk({"researcher": Model()}, max_delegates=2)

    outcome = await pipeline(office).call(
        "delegate", {"briefs": [{"brief": "b", "role": "researcher"}] * 3}
    )

    assert outcome.status == "invalid"
    assert "briefs" in outcome.reason


async def test_a_result_that_is_not_json_still_comes_back_as_its_summary() -> None:
    office = desk({"researcher": Model(said("It rains at three."))})

    outcome = await pipeline(office).call(
        "delegate", {"briefs": [{"brief": "b", "role": "researcher"}]}
    )

    assert "It rains at three." in outcome.text
    assert office.results == [StepResult(summary="It rains at three.", findings=[], sources=[])]


async def test_review_asks_the_critic_about_the_draft() -> None:
    critic = Model(said("Issue: no source for the 15:00 claim."))
    office = desk({"critic": critic})

    outcome = await pipeline(office).call("review", {"draft": "Rain at 15:00.", "request": "Rain?"})

    assert outcome.text == "Issue: no source for the 15:00 claim."
    assert "Rain at 15:00." in critic.seen[0][-1]["content"]


async def test_a_team_run_is_proposed_to_the_owner_never_started() -> None:
    office = desk({})

    outcome = await pipeline(office).call(
        "propose_team_run", {"objective": "Full market study of NL e-bikes", "why": "Many sources"}
    )

    assert office.proposals == [
        {"objective": "Full market study of NL e-bikes", "why": "Many sources"}
    ]
    assert "owner decides" in outcome.text


async def test_the_lead_is_told_start_building_comes_only_after_the_team_plans() -> None:
    """A lead once told the owner to press Start building on a task where no build
    existed yet: only a proposal, started with its own button."""
    outcome = await pipeline(desk({})).call(
        "propose_team_run", {"objective": "Build a Slack self-check tool", "why": "Code"}
    )

    assert "nothing has started yet" in outcome.text
    assert "do not say Start building is there to press now" in outcome.text


def test_each_tool_says_when_not_to_use_it() -> None:
    specs = {spec.name: spec for spec in lead_tools([SEARCH], desk({}))}

    assert "Do not" in specs["delegate"].description
    assert "Do not" in specs["review"].description
    assert "only" in specs["propose_team_run"].description


async def test_each_new_step_list_is_reported_while_the_lead_works() -> None:
    seen: list[list[str]] = []
    office = desk({}, on_todo=lambda items: seen.append([i.status for i in items]))

    await pipeline(office).call(
        "todo", {"items": [{"text": "Find sources", "status": "in_progress"}]}
    )

    assert seen == [["in_progress"]]


async def test_each_hand_off_is_told_to_the_run_typed() -> None:
    """Q4, Stage 5e: the run keeps each brief's result, not its transcript."""
    told: list[tuple[str, str, list[str]]] = []
    office = desk({"researcher": Model(searching("rain"), said(RESULT))})
    office.on_result = lambda brief, result: told.append(
        (brief.brief, result.summary, result.sources)
    )

    await pipeline(office).call("delegate", {"briefs": [{"brief": "Rain?", "role": "researcher"}]})

    assert told == [("Rain?", "Rain from 15:00.", ["https://knmi.nl"])]


async def test_a_brief_whose_model_failed_says_so_to_the_lead() -> None:
    office = desk({"researcher": Model(GatewayError("provider_unavailable"))})

    outcome = await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    assert "did not finish" in outcome.text and "provider_unavailable" in outcome.text
    assert office.results[0].status == "failed"
    assert office.results[0].reason == "provider_unavailable"


async def test_a_brief_stopped_by_a_limit_says_so_and_keeps_what_it_has() -> None:
    researcher = Model(searching("rain"), GatewayError("budget_exhausted:run"))
    office = desk({"researcher": researcher})

    outcome = await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    assert office.results[0].status == "limit"
    assert "budget_exhausted:run" in outcome.text and "result for rain" in outcome.text


async def test_a_model_cannot_report_its_own_brief_as_finished() -> None:
    said_done = json.dumps({"summary": "s", "status": "failed", "reason": "made up"})
    office = desk({"researcher": Model(said(said_done))})

    await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    assert office.results[0].status == "done" and office.results[0].reason == ""


async def test_an_address_no_tool_returned_does_not_reach_the_lead() -> None:
    invented = json.dumps(
        {
            "summary": "See https://invented.example/a.",
            "findings": ["Rain, https://invented.example/b", "Rain, https://knmi.nl"],
            "sources": ["https://invented.example/c", "https://knmi.nl"],
        }
    )
    office = desk({"researcher": Model(searching("rain"), said(invented))})

    outcome = await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    assert "invented.example" not in outcome.text
    assert "https://knmi.nl" in outcome.text
    assert "3 addresses" in outcome.text
    assert office.results[0].sources == ["https://knmi.nl"]
    assert len(office.results[0].unreturned) == 3


async def test_a_brief_that_called_no_tool_cannot_supply_a_citation() -> None:
    office = desk({"researcher": Model(said(RESULT))})

    outcome = await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    conversation = [{"role": "tool", "content": outcome.text}]
    assert check_citations("Rain, https://knmi.nl", conversation).unresolved == ["https://knmi.nl"]


async def test_a_stop_reaches_a_brief_before_its_next_model_call() -> None:
    researcher = Model(said(RESULT))
    office = desk({"researcher": researcher}, halt=lambda: "cancel")

    await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    assert researcher.seen == [], "no call was made after the stop"
    assert office.results[0].status == "stopped" and office.results[0].reason == "cancel"


async def test_a_briefs_tool_calls_are_reported_with_its_number() -> None:
    steps: list[dict[str, Any]] = []

    async def step(event: dict[str, Any]) -> None:
        steps.append(event)

    office = desk({"researcher": Model(searching("rain"), said(RESULT))}, on_step=step)

    await pipeline(office).call("delegate", {"briefs": [{"brief": "b"}]})

    assert [(s["kind"], s["tool"], s["brief"]) for s in steps] == [("tool_call", "search", 1)]


async def test_a_spent_budget_stops_the_other_briefs() -> None:
    finished: list[str] = []

    async def spent(arguments: dict[str, Any]) -> ToolResult:
        raise GatewayError("budget_exhausted:run")

    async def slow(arguments: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(0.2)
        finished.append("slow")
        return ToolResult(text="late")

    class Either(Model):
        async def complete(
            self, model_alias: str, messages: list[dict[str, Any]], **options: Any
        ) -> Any:
            wanted = "search" if "first" in messages[-1]["content"] else "wait"
            return Completion(
                model_alias="m",
                model="m",
                content="",
                usage=Usage(),
                latency_seconds=0.0,
                tool_calls=[
                    ToolCall(
                        id="c",
                        name=wanted,
                        arguments={"query": "q"},
                        raw_arguments='{"query": "q"}',
                    )
                ],
            )

    tools = [
        ToolSpec("search", "s", spent, Query, concurrency_safe=True),
        ToolSpec("wait", "w", slow, Query, concurrency_safe=True),
    ]
    office = desk({"researcher": Either()})
    both = [{"brief": "first"}, {"brief": "second"}]

    with pytest.raises(GatewayError):
        await ToolPipeline(lead_tools(tools, office)).call("delegate", {"briefs": both})
    await asyncio.sleep(0.3)

    assert finished == [], "the other brief was cancelled, not left spending"


async def test_an_owner_question_in_a_brief_stops_the_lead_for_the_owner() -> None:
    question = PolicyAsk(Decision(Verdict.ASK, "needs the owner", "mcp"), "record")

    async def asks(arguments: dict[str, Any]) -> ToolResult:
        raise question

    tools = [ToolSpec("search", "s", asks, Query, concurrency_safe=True)]
    office = desk({"researcher": Model(searching("rain"))})

    outcome = await ToolPipeline(lead_tools(tools, office)).call(
        "delegate", {"briefs": [{"brief": "b"}]}
    )

    assert outcome.status == "ask" and outcome.error is question


async def test_a_full_brief_gives_the_agent_its_output_sources_and_boundaries() -> None:
    researcher = Model(said(RESULT))
    office = desk({"researcher": researcher})
    brief = {
        "brief": "Find the NL VAT rate.",
        "output": "The rate and its date.",
        "sources": "Belastingdienst.",
        "boundaries": "Not Belgium.",
    }

    await pipeline(office).call("delegate", {"briefs": [brief]})

    asked = researcher.seen[0][-1]["content"]
    for part in brief.values():
        assert part in asked


def test_the_tools_say_what_they_return_and_when_to_use_them() -> None:
    specs = {spec.name: spec for spec in lookup_specs(None, None, None)}

    assert "snippet" in specs["search"].description
    assert "broad" in specs["search"].description
    assert "read_more" in specs["read"].description
    assert "only when" in specs["look"].description


async def test_a_brief_stops_at_its_step_limit_and_says_so() -> None:
    model = Model(*[searching(f"query {n}") for n in range(BRIEF_STEPS + 2)])
    office = desk({"researcher": model})

    await pipeline(office).call("delegate", {"briefs": [{"brief": "Never ends"}]})

    assert len(model.seen) == BRIEF_STEPS
    assert (office.results[0].status, office.results[0].reason) == ("limit", "step_limit")


def test_a_note_reaches_every_reader_once() -> None:
    waiting = ["Only Dutch banks"]

    def inbox() -> list[str]:
        taken = list(waiting)
        waiting.clear()
        return taken

    notes = SharedNotes(inbox)
    lead, brief_1, brief_2 = notes.reader(), notes.reader(), notes.reader()

    assert brief_1() == ["Only Dutch banks"]
    assert brief_1() == []
    assert brief_2() == ["Only Dutch banks"]
    assert lead() == ["Only Dutch banks"]


async def test_a_note_sent_during_briefs_reaches_each_of_them() -> None:
    waiting = ["Only Dutch banks"]

    def inbox() -> list[str]:
        taken = list(waiting)
        waiting.clear()
        return taken

    first, second = Model(said("one")), Model(said("two"))
    models = iter((first, second))

    def completer_for(role: str) -> tuple[Any, str]:
        return next(models), role

    office = LeadDesk(completer_for=completer_for, notes=SharedNotes(inbox))
    await pipeline(office).call("delegate", {"briefs": [{"brief": "A"}, {"brief": "B"}]})

    for model in (first, second):
        assert "Only Dutch banks" in str(model.seen[0])
