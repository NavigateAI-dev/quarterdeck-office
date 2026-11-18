"""The critic checking claims with the tools the owner granted."""

import json
from typing import Any
from uuid import uuid4

from app.agents import CriticAgent
from app.events import InvocationRecorder
from app.mcp import review as mcp_review
from app.mcp.calling import McpTools, Reachable
from tests.unit.test_mcp_research import (
    Gate,
    Gateway,
    Transport,
    choice,
    config,
    context,
    tool,
)

CHECK = {"server": "Filings", "tool": "search_filings", "arguments": {"query": "wwft art 3"}}


def critique() -> str:
    return json.dumps({"verdict": "PASS", "confidence": "medium"})


def granted(stances: dict[str, str], transport: Transport | None = None) -> McpTools:
    tools = [tool(name) for name in stances]
    review: dict[str, Any] = {}
    for offered in tools:
        review = mcp_review.decide(review, offered, stances[offered["name"]], "Alex")
    return McpTools(
        servers=[
            Reachable(
                server_id=uuid4(),
                name="Filings",
                url="https://mcp.example.com/mcp",
                token=None,
                tools=tools,
                review=review,
            )
        ],
        gate=Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=transport or Transport(),  # type: ignore[arg-type]
    )


def critic(gateway: Gateway, tools: McpTools | None, **over: Any) -> CriticAgent:
    agent = CriticAgent(gateway, config(), over.get("recorder"))  # type: ignore[arg-type]
    agent.mcp = tools
    return agent


async def test_without_tools_the_critic_makes_one_completion() -> None:
    gateway = Gateway([critique()])
    await critic(gateway, None).run("Does the Wwft require CDD?", context())
    assert len(gateway.prompts) == 1


async def test_a_check_reaches_the_critique_prompt() -> None:
    transport = Transport()
    gateway = Gateway([choice(CHECK), critique()])
    await critic(gateway, granted({"search_filings": mcp_review.ALLOWED}, transport)).run(
        "Does the Wwft require CDD?", context()
    )
    assert transport.calls == [("search_filings", {"query": "wwft art 3"})]
    assert "Filing 2025-17" in gateway.prompts[-1]
    assert "contradict" in gateway.prompts[-1].lower()


async def test_a_tool_needing_approval_is_not_offered_to_the_critic() -> None:
    transport = Transport()
    tools = granted({"search_filings": mcp_review.NEEDS_APPROVAL}, transport)
    gateway = Gateway([critique()])
    await critic(gateway, tools).run("q", context())
    # Nothing on offer: no choice is asked for, and nothing is called.
    assert len(gateway.prompts) == 1
    assert transport.calls == []


async def test_a_needs_approval_tool_is_never_called_even_if_chosen() -> None:
    transport = Transport()
    stances = {"search_filings": mcp_review.ALLOWED, "file_issue": mcp_review.NEEDS_APPROVAL}
    tools = granted(stances, transport)
    chosen = {"server": "Filings", "tool": "file_issue", "arguments": {}}
    gateway = Gateway([choice(chosen), critique()])
    await critic(gateway, tools).run("q", context())
    assert "file_issue" not in gateway.prompts[0]
    assert transport.calls == []


async def test_a_refused_check_does_not_fail_the_critique() -> None:
    from app.policies.engine import Verdict

    tools = granted({"search_filings": mcp_review.ALLOWED})
    tools.gate = Gate(Verdict.DENY)
    gateway = Gateway([choice(CHECK), critique()])
    result = await critic(gateway, tools).run("q", context())
    assert result.verdict == "PASS"


async def test_the_critic_makes_at_most_two_checks() -> None:
    transport = Transport()
    many = [{**CHECK, "arguments": {"query": f"q{n}"}} for n in range(5)]
    gateway = Gateway([choice(*many), critique()])
    await critic(gateway, granted({"search_filings": mcp_review.ALLOWED}, transport)).run(
        "q", context()
    )
    assert len(transport.calls) == 2


async def test_the_critics_choice_is_billed() -> None:
    recorder = InvocationRecorder()
    gateway = Gateway([choice(), critique()])
    await critic(gateway, granted({"search_filings": mcp_review.ALLOWED}), recorder=recorder).run(
        "q", context()
    )
    assert len(recorder.events) == 2
