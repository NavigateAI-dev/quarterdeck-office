"""The lead agent's loop.

One model, with native tools, in a loop: it answers, or calls tools and reads
what they return, until it answers or the owner's limits stop it.
"""

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from app.gateway.client import Completion, GatewayError, ToolCall, Usage
from app.lead.loop import REPEAT_REMINDERS, run_lead, tool_names
from app.lead.spill import HEAD_CHARS, SPILL_CHARS, Spill
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk
from app.tools.pipeline import ToolPipeline, ToolResult, ToolSpec


def said(text: str) -> Completion:
    return Completion(
        model_alias="lead", model="m", content=text, usage=Usage(), latency_seconds=0.0
    )


def calls(*made: tuple[str, dict[str, Any] | None]) -> Completion:
    return Completion(
        model_alias="lead",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[
            ToolCall(
                id=f"call_{n}",
                name=name,
                arguments=arguments,
                raw_arguments=json.dumps(arguments) if arguments is not None else "query=rain",
            )
            for n, (name, arguments) in enumerate(made)
        ],
    )


class Model:
    """A model scripted turn by turn; it records what each turn was shown."""

    def __init__(self, *turns: Completion | Exception) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []
        self.offered: list[list[dict[str, Any]]] = []

    async def complete(
        self, model_alias: str, messages: list[dict[str, Any]], **options: Any
    ) -> Completion:
        self.seen.append([dict(m) for m in messages])
        self.offered.append(options.get("tools") or [])
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class Query(BaseModel):
    query: str


def search_tool(results: dict[str, str] | None = None, *, safe: bool = True) -> ToolSpec:
    async def run(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            text=(results or {}).get(arguments["query"], f"found {arguments['query']}")
        )

    return ToolSpec("search", "A web search.", run, Query, concurrency_safe=safe)


START = [{"role": "system", "content": "You are the lead."}, {"role": "user", "content": "Rain?"}]


async def lead(model: Model, *specs: ToolSpec, **options: Any) -> Any:
    return await run_lead(model, "lead", list(START), ToolPipeline(specs), **options)


async def test_a_question_needing_no_tool_is_answered_in_one_call() -> None:
    model = Model(said("No rain today."))

    result = await lead(model, search_tool())

    assert (result.answer, result.stopped) == ("No rain today.", "answered")
    assert len(model.seen) == 1


async def test_the_model_is_offered_the_tools_with_their_argument_schemas() -> None:
    model = Model(said("ok"))

    await lead(model, search_tool())

    offered = {tool["name"]: tool for tool in model.offered[0]}
    assert set(offered) == {"search", "read_more"}
    assert offered["search"]["parameters"]["required"] == ["query"]


async def test_a_tool_result_comes_back_to_the_model_and_it_answers_from_it() -> None:
    model = Model(calls(("search", {"query": "rain Lyon"})), said("Rain at 15:00."))

    result = await lead(model, search_tool({"rain Lyon": "KNMI: rain at 15:00"}))

    assert result.answer == "Rain at 15:00."
    second = model.seen[1]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "search"
    assert second[-1] == {
        "role": "tool",
        "tool_call_id": "call_0",
        "content": "KNMI: rain at 15:00",
    }


async def test_safe_calls_run_at_once_and_results_keep_the_model_order() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def run(arguments: dict[str, Any]) -> ToolResult:
        started.append(arguments["query"])
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 1)
        return ToolResult(text=arguments["query"])

    spec = ToolSpec("search", "A web search.", run, Query, concurrency_safe=True)
    model = Model(calls(("search", {"query": "a"}), ("search", {"query": "b"})), said("done"))

    await lead(model, spec)

    assert [m["content"] for m in model.seen[1] if m["role"] == "tool"] == ["a", "b"]


async def test_unsafe_calls_run_one_after_another() -> None:
    running = 0
    most = 0

    async def run(arguments: dict[str, Any]) -> ToolResult:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return ToolResult(text="ok")

    spec = ToolSpec("post", "Post a message.", run, Query, side_effect=True)
    model = Model(calls(("post", {"query": "a"}), ("post", {"query": "b"})), said("done"))

    await lead(model, spec)

    assert most == 1


async def test_bad_arguments_are_told_to_the_model_by_field_and_it_can_retry() -> None:
    model = Model(
        calls(("search", {"q": "rain"})), calls(("search", {"query": "rain"})), said("ok")
    )

    await lead(model, search_tool())

    feedback = model.seen[1][-1]["content"]
    assert feedback.startswith("Invalid arguments")
    assert "query: Field required" in feedback


async def test_arguments_that_are_not_json_are_told_to_the_model() -> None:
    model = Model(calls(("search", None)), said("ok"))

    await lead(model, search_tool())

    assert "not a JSON object" in model.seen[1][-1]["content"]


async def test_a_tool_that_is_not_offered_is_refused_and_said() -> None:
    model = Model(calls(("delete_everything", {})), said("ok"))

    await lead(model, search_tool())

    assert model.seen[1][-1]["content"].startswith("Refused: tool_not_offered")


async def test_a_tool_that_fails_is_told_to_the_model_and_the_answer_goes_on() -> None:
    class PageNotRead(Exception):
        pass

    async def run(arguments: dict[str, Any]) -> ToolResult:
        raise PageNotRead("fetch_rejected_404")

    broken = ToolSpec("read", "Read a page.", run, Query)
    model = Model(calls(("read", {"query": "https://example.org/gone"})), said("It is gone."))

    result = await lead(model, broken)

    assert (result.answer, result.stopped) == ("It is gone.", "answered")
    assert model.seen[1][-1]["content"] == "Failed: fetch_rejected_404."


async def test_a_paid_tool_past_the_budget_still_stops_the_work() -> None:
    async def run(arguments: dict[str, Any]) -> ToolResult:
        raise GatewayError("budget_exhausted:cost")

    paid = ToolSpec("filings", "Paid filings search.", run, Query)
    model = Model(calls(("filings", {"query": "acme"})), said("unreachable"))

    with pytest.raises(GatewayError, match="budget_exhausted"):
        await lead(model, paid)


async def test_an_owner_question_stops_the_loop_for_the_owner() -> None:
    asked = PolicyAsk(Decision(Verdict.ASK, "ask the owner", "crm"), 1)

    async def run(arguments: dict[str, Any]) -> ToolResult:
        raise asked

    spec = ToolSpec("crm", "The CRM.", run)
    model = Model(calls(("crm", {})), said("never reached"))

    result = await lead(model, spec)

    assert result.stopped == "asked"
    assert result.asked is asked
    assert len(model.seen) == 1


async def test_a_spent_budget_ends_with_the_best_answer_so_far_and_says_why() -> None:
    model = Model(calls(("search", {"query": "rain"})), GatewayError("budget_exhausted:cost"))

    result = await lead(model, search_tool())

    assert result.stopped == "limit"
    assert result.reason == "budget_exhausted:cost"
    assert "found rain" in result.answer


async def test_a_failed_provider_ends_the_loop_with_its_code() -> None:
    result = await lead(Model(GatewayError("provider_unavailable")), search_tool())

    assert (result.stopped, result.reason) == ("error", "provider_unavailable")


async def test_the_same_call_repeated_gets_a_reminder_at_three_five_and_eight() -> None:
    same = calls(("search", {"query": "rain"}))
    model = Model(*[same] * 8, said("ok"))

    await lead(model, search_tool())

    reminded = [
        n + 1 for n, turn in enumerate(model.seen[1:]) if "same call" in turn[-1]["content"]
    ]
    assert reminded == list(REPEAT_REMINDERS)


async def test_an_owner_step_limit_ends_the_loop() -> None:
    same = calls(("search", {"query": "rain"}))
    model = Model(*[same] * 5, said("never"))

    result = await lead(model, search_tool(), max_steps=3)

    assert (result.stopped, result.reason) == ("limit", "step_limit")
    assert len(model.seen) == 3


async def test_a_long_result_is_spilled_with_head_tail_and_a_reference() -> None:
    long = "A" * HEAD_CHARS + "B" * (SPILL_CHARS * 2) + "Z" * 100
    model = Model(calls(("search", {"query": "x"})), said("ok"))

    await lead(model, search_tool({"x": long}))

    shown = model.seen[1][-1]["content"]
    assert len(shown) < SPILL_CHARS
    assert shown.startswith("A" * 100) and shown.rstrip().endswith("Z" * 100)
    assert "read_more" in shown and "ref=" in shown


async def test_read_more_opens_a_spilled_result_further() -> None:
    long = "x" * (SPILL_CHARS + 5000) + "THE END"
    model = Model(calls(("search", {"query": "x"})), said("ok"))
    spill = Spill()
    result = await lead(model, search_tool({"x": long}), spill=spill)
    [ref] = list(spill.stored)

    more = await ToolPipeline([spill.read_more_spec()]).call(
        "read_more", {"ref": ref, "offset": len(long) - 7, "length": 100}
    )

    assert result.stopped == "answered"
    assert more.text == "THE END"


def test_tool_names_are_made_safe_for_every_provider_and_stay_unique() -> None:
    names = tool_names(["mcp:Boards:list", "mcp:Boards!:list", "search", "x" * 80])

    assert names["mcp:Boards:list"] == "mcp_Boards_list"
    assert names["mcp:Boards!:list"] == "mcp_Boards_list_2"
    assert names["search"] == "search"
    assert len(names["x" * 80]) == 64
    assert all(name.replace("_", "").replace("-", "").isalnum() for name in names.values())


async def test_each_step_is_reported_as_it_happens() -> None:
    steps: list[dict[str, Any]] = []
    model = Model(calls(("search", {"query": "rain"})), said("ok"))

    async def step(event: dict[str, Any]) -> None:
        steps.append(event)

    await lead(model, search_tool(), on_step=step)

    assert [s["kind"] for s in steps] == ["tool_call", "answer"]
    assert steps[0]["tool"] == "search" and steps[0]["status"] == "ok"


@pytest.mark.parametrize("stopped", ["answered"])
async def test_the_result_carries_the_whole_conversation(stopped: str) -> None:
    model = Model(calls(("search", {"query": "rain"})), said("ok"))

    result = await lead(model, search_tool())

    assert result.stopped == stopped
    assert [m["role"] for m in result.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


async def test_a_picture_a_tool_returned_is_shown_to_a_model_that_sees() -> None:
    async def look(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(text="Buienradar: droog", picture=b"\xff\xd8radar")

    spec = ToolSpec("look", "Look at a page.", look, Query)
    model = Model(calls(("look", {"query": "radar"})), said("Dry."))

    await lead(model, spec, sees=True)

    shown = model.seen[1][-1]
    assert shown["role"] == "user"
    assert shown["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_a_model_that_does_not_see_gets_words_only() -> None:
    async def look(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(text="Buienradar: droog", picture=b"\xff\xd8radar")

    model = Model(calls(("look", {"query": "radar"})), said("Dry."))

    await lead(model, ToolSpec("look", "Look at a page.", look, Query))

    assert model.seen[1][-1]["role"] == "tool"


async def test_a_note_sent_while_it_works_reaches_the_next_model_call() -> None:
    notes = [[], ["Focus on B2B service firms."]]
    model = Model(calls(("search", {"query": "rain"})), said("ok"))
    steps: list[dict[str, Any]] = []

    async def step(event: dict[str, Any]) -> None:
        steps.append(event)

    await lead(model, search_tool(), inbox=lambda: notes.pop(0) if notes else [], on_step=step)

    assert model.seen[1][-1] == {
        "role": "user",
        "content": "The owner, while you work: Focus on B2B service firms.",
    }
    assert {"kind": "steered", "count": 1} in steps


async def test_a_pause_or_cancel_stops_the_loop_before_the_next_call() -> None:
    asked = ["", "cancel"]
    model = Model(calls(("search", {"query": "rain"})), said("never"))

    result = await lead(model, search_tool(), halt=lambda: asked.pop(0) or None)

    assert (result.stopped, result.reason) == ("halted", "cancel")
    assert len(model.seen) == 1
