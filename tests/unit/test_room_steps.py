"""The steps behind a reply in a discussion, and what it rests on."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.gateway.client import Completion, ToolCall, Usage
from app.lead.service import LeadRequest, answer
from app.room_steps import StepLog, sources_action
from app.tools.pipeline import ToolResult, ToolSpec


class Address(BaseModel):
    address: str


def said(text: str) -> Completion:
    return Completion(
        model_alias="lead", model="m", content=text, usage=Usage(), latency_seconds=0.0
    )


def reading(address: str) -> Completion:
    return Completion(
        model_alias="lead",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[ToolCall(id="1", name="read", arguments={"address": address})],
    )


class Model:
    def __init__(self, *turns: Completion) -> None:
        self.turns = list(turns)

    async def complete(self, alias: str, messages: list[Any], **options: Any) -> Completion:
        return self.turns.pop(0)


async def replied(log: StepLog, model: Model, failing: bool = False) -> Any:
    async def run(arguments: dict[str, Any]) -> ToolResult:
        if failing:
            raise RuntimeError("page_timeout")
        return ToolResult(text=f"page at {arguments['address']}", source=arguments["address"])

    return await answer(
        LeadRequest(
            request="q",
            completer_for=lambda role: (model, "lead"),
            work=[ToolSpec("read", "Read.", run, Address, concurrency_safe=True)],
            now=datetime.now(UTC),
            on_step=log.step,
            on_start=log.started,
        )
    )


async def test_a_step_shows_as_running_until_its_tool_comes_back() -> None:
    log = StepLog()

    await log.started({"kind": "tools_started", "tools": ["read"]})
    assert log.steps == [{"tool": "read", "status": "running"}]

    await log.step({"kind": "tool_call", "tool": "read", "status": "ok", "source": "https://a.nl"})
    assert log.steps[0]["status"] == "ok" and log.steps[0]["source"] == "https://a.nl"


async def test_a_real_reply_records_what_was_read_and_where() -> None:
    log = StepLog()

    await replied(log, Model(reading("https://a.nl/x"), said("See https://a.nl/x")))

    assert [(s["tool"], s["status"], s["source"]) for s in log.kept()] == [
        ("read", "ok", "https://a.nl/x")
    ]


async def test_a_failed_tool_is_kept_as_failed_with_its_reason() -> None:
    log = StepLog()

    await replied(log, Model(reading("https://a.nl/x"), said("It would not load.")), failing=True)

    assert log.kept()[0]["status"] == "failed"
    assert "page_timeout" in log.kept()[0]["reason"]


async def test_a_step_still_running_when_the_reply_came_is_kept_as_stopped() -> None:
    log = StepLog()

    await log.started({"tools": ["read"]})

    assert log.kept()[0]["status"] == "stopped"


async def test_a_connected_tool_is_left_to_the_line_that_names_its_server() -> None:
    log = StepLog()

    await log.started({"tools": ["mcp:Miro:board_create", "read"]})

    assert [s["tool"] for s in log.steps] == ["mcp:Miro:board_create", "read"], (
        "shown while it runs"
    )
    assert [s["tool"] for s in log.kept()] == ["read"]


async def test_an_address_no_tool_returned_is_never_a_source() -> None:
    log = StepLog()
    model = Model(
        reading("https://a.nl/x"),
        said("See https://a.nl/x and https://made-up.example"),
        said("See https://a.nl/x and https://made-up.example"),
    )

    reply = await replied(log, model)
    action = sources_action(reply.citations.cited, reply.citations.unresolved, log)

    assert action is not None and action["addresses"] == ["https://a.nl/x"]


async def test_a_reply_resting_on_nothing_has_no_sources() -> None:
    assert sources_action([], [], StepLog()) is None


async def test_only_a_conversation_really_read_is_a_source() -> None:
    log = StepLog()
    await log.step({"kind": "tool_call", "tool": "search_memory", "status": "ok"})
    await log.step(
        {"kind": "tool_call", "tool": "get_conversation", "status": "ok", "source": "archive:1"}
    )
    await log.step({"kind": "tool_call", "tool": "get_conversation", "status": "failed"})

    action = sources_action([], [], log)

    assert action is not None and action["conversations"] == ["archive:1"]
