"""Native tool calls through the Completer stack.

Every layer passes `tools` on only when it is given, so a completer that has
never heard of tools keeps working, and a call without tools sends what it
sent before.
"""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest

from app.budget import Budget, BudgetedCompleter, RunLedger
from app.failover import Breaker, FailoverCompleter
from app.gateway.client import ToolCall
from app.gateway.direct import DirectCompleter
from app.policies.guard import GuardedCompleter
from tests.unit.test_direct_completion import KEY, settings

TOOLS = [
    {
        "name": "search",
        "description": "A web search query.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
]
# A conversation that already made one call and has its result.
TURNS: list[dict[str, Any]] = [
    {"role": "system", "content": "Answer briefly."},
    {"role": "user", "content": "Rain in Lyon?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"query": "rain Lyon"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "- KNMI: rain at 15:00"},
]


def reply(content: str = "OK", **overrides: object) -> dict[str, object]:
    return {
        "model": "resolved-provider-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        **overrides,
    }


def calling(arguments: str = '{"query": "rain"}') -> dict[str, Any]:
    message = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {"name": "search", "arguments": arguments},
            }
        ],
    }
    return reply(choices=[{"message": message, "finish_reason": "tool_calls"}])


def openai_direct(seen: list[Any], message: dict[str, Any], finish: str) -> DirectCompleter:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5-2026",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", **message},
                        "finish_reason": finish,
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            },
        )

    return DirectCompleter(
        "openai",
        "gpt-5",
        KEY,
        settings(),
        label="connection:o",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


async def test_openai_direct_offers_tools_and_reads_the_calls() -> None:
    seen: list[Any] = []
    message = calling()["choices"][0]["message"]  # type: ignore[index]

    completion = await openai_direct(seen, message, "tool_calls").complete("x", TURNS, tools=TOOLS)

    assert seen[0]["tools"] == [{"type": "function", "function": TOOLS[0]}]
    assert seen[0]["messages"] == TURNS
    assert [c.name for c in completion.tool_calls] == ["search"]


async def test_arguments_that_are_not_a_json_object_are_kept_raw_for_feedback() -> None:
    message = calling("query=rain")["choices"][0]["message"]  # type: ignore[index]

    completion = await openai_direct([], message, "tool_calls").complete("x", TURNS, tools=TOOLS)

    [call] = completion.tool_calls
    assert call.arguments is None
    assert call.raw_arguments == "query=rain"


async def test_openai_direct_without_tools_sends_no_tools() -> None:
    seen: list[Any] = []

    await openai_direct(seen, {"content": "Sunny."}, "stop").complete("x", TURNS[:2])

    assert "tools" not in seen[0]


def anthropic_direct(seen: list[Any], content: list[dict[str, Any]], stop: str) -> DirectCompleter:
    def handle(request: Any) -> Any:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": content,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )

    return DirectCompleter(
        "anthropic",
        "claude-opus-5",
        KEY,
        settings(),
        label="connection:a",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )


async def test_anthropic_direct_offers_tools_and_reads_tool_use() -> None:
    seen: list[Any] = []
    use = {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "rain"}}

    completion = await anthropic_direct(seen, [use], "tool_use").complete("x", TURNS, tools=TOOLS)

    assert seen[0]["tools"] == [
        {
            "name": "search",
            "description": "A web search query.",
            "input_schema": TOOLS[0]["parameters"],
        }
    ]
    assert completion.content == ""
    assert completion.tool_calls == [
        ToolCall(
            id="toolu_1",
            name="search",
            arguments={"query": "rain"},
            raw_arguments='{"query": "rain"}',
        )
    ]


async def test_anthropic_direct_turns_tool_turns_into_tool_use_and_results() -> None:
    seen: list[Any] = []

    await anthropic_direct(seen, [{"type": "text", "text": "Rain at 15:00."}], "end_turn").complete(
        "x", TURNS, tools=TOOLS
    )

    assert seen[0]["messages"] == [
        {"role": "user", "content": "Rain in Lyon?"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "search",
                    "input": {"query": "rain Lyon"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "- KNMI: rain at 15:00"}
            ],
        },
    ]


async def test_anthropic_direct_without_tools_sends_no_tools() -> None:
    seen: list[Any] = []

    await anthropic_direct(seen, [{"type": "text", "text": "Sunny."}], "end_turn").complete(
        "x", TURNS[:2]
    )

    assert "tools" not in seen[0]


class Recorder:
    """A completer that notes what each call was given."""

    def __init__(self) -> None:
        self.given: list[dict[str, Any]] = []

    async def complete(self, model_alias: str, messages: Any, **options: Any) -> Any:
        self.given.append(options)
        return SimpleNamespace(
            content="",
            tool_calls=[],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            estimated_cost=None,
        )


class Gate:
    def enforce(self, *args: Any, **kwargs: Any) -> None:
        return None


def layers(inner: Recorder) -> list[Any]:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=500)
    return [
        BudgetedCompleter(inner, ledger),  # type: ignore[arg-type]
        GuardedCompleter(inner, gate=Gate(), run_id=None, project_id=None, role="chief"),
        FailoverCompleter(inner, key="k", backup_key="b", make_backup=Recorder, breaker=Breaker()),
    ]


@pytest.mark.parametrize("index", [0, 1, 2], ids=["budget", "guard", "failover"])
async def test_each_layer_passes_tools_on(index: int) -> None:
    inner = Recorder()

    await layers(inner)[index].complete("chief", TURNS, tools=TOOLS)

    assert inner.given[-1]["tools"] == TOOLS


@pytest.mark.parametrize("index", [0, 1, 2], ids=["budget", "guard", "failover"])
async def test_a_layer_called_without_tools_passes_none_on(index: int) -> None:
    inner = Recorder()

    await layers(inner)[index].complete("chief", TURNS[:2])

    assert "tools" not in inner.given[-1]


def test_the_budget_counts_the_tool_catalogue_and_tool_turns() -> None:
    ledger = RunLedger(Budget(max_tokens=100_000), max_output_tokens=0)
    plain = ledger.reserve(TURNS[:2], 1)
    ledger.release(plain)

    with_tools = ledger.reserve(TURNS, 1, tools=TOOLS)

    assert with_tools > plain + len(json.dumps(TOOLS)) // 4 - 1
