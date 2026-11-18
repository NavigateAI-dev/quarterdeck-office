"""ToolPipeline: the one way a tool is found, checked and run."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from app.mcp.calling import ApprovalRequired, ToolRefused
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk, PolicyRefused
from app.tools.pipeline import ToolPipeline, ToolResult, ToolSpec


class Query(BaseModel):
    query: str
    limit: int = 3


def spec(run: Any, **fields: Any) -> ToolSpec:
    return ToolSpec(name="search", description="Search the web", run=run, **fields)


async def echo(arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(text=repr(sorted(arguments.items())))


async def test_runs_the_named_tool_with_its_arguments() -> None:
    pipeline = ToolPipeline([spec(echo)])

    outcome = await pipeline.call("search", {"query": "rain"})

    assert outcome.status == "ok"
    assert outcome.text == "[('query', 'rain')]"


async def test_an_unknown_tool_is_refused_without_running_anything() -> None:
    outcome = await ToolPipeline([spec(echo)]).call("delete_everything", {})

    assert (outcome.status, outcome.reason) == ("refused", "tool_not_offered")


async def test_arguments_are_checked_against_the_input_model_and_defaults_filled() -> None:
    pipeline = ToolPipeline([spec(echo, input_model=Query)])

    outcome = await pipeline.call("search", {"query": "rain"})

    assert outcome.text == "[('limit', 3), ('query', 'rain')]"


async def test_invalid_arguments_never_reach_the_tool_and_name_the_field() -> None:
    ran: list[dict[str, Any]] = []

    async def run(arguments: dict[str, Any]) -> ToolResult:
        ran.append(arguments)
        return ToolResult(text="")

    pipeline = ToolPipeline([spec(run, input_model=Query)])

    outcome = await pipeline.call("search", {"limit": "many"})

    assert ran == []
    assert outcome.status == "invalid"
    assert "query: Field required" in outcome.reason
    assert "limit: Input should be a valid integer" in outcome.reason


async def test_a_slow_tool_fails_on_its_timeout() -> None:
    async def slow(arguments: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult(text="late")

    outcome = await ToolPipeline([spec(slow, timeout_s=0.01)]).call("search", {})

    assert (outcome.status, outcome.reason) == ("failed", "tool_timeout")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ToolRefused("tool_refused"), "tool_refused"),
        (ApprovalRequired("crm", "search"), "approval_required"),
        (PolicyRefused(Decision(Verdict.DENY, "no"), None), "policy_refused"),
    ],
)
async def test_a_refusal_becomes_a_refused_outcome(error: Exception, reason: str) -> None:
    async def refuse(arguments: dict[str, Any]) -> ToolResult:
        raise error

    outcome = await ToolPipeline([spec(refuse)]).call("search", {})

    assert (outcome.status, outcome.reason) == ("refused", reason)
    assert outcome.error is error


async def test_an_owner_question_is_kept_for_the_caller_to_raise_or_propose() -> None:
    asked = PolicyAsk(Decision(Verdict.ASK, "ask the owner", "crm"), 1)

    async def ask(arguments: dict[str, Any]) -> ToolResult:
        raise asked

    outcome = await ToolPipeline([spec(ask)]).call("search", {})

    assert outcome.status == "ask"
    assert outcome.error is asked


async def test_an_unexpected_error_is_not_hidden() -> None:
    async def broken(arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await ToolPipeline([spec(broken)]).call("search", {})


def test_two_tools_with_one_name_are_refused_at_build() -> None:
    with pytest.raises(ValueError, match="search"):
        ToolPipeline([spec(echo), spec(echo)])


def test_a_spec_describes_its_arguments_for_the_model() -> None:
    schema = spec(echo, input_model=Query).input_schema

    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query", "limit"}
