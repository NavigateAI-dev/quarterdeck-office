"""The researcher calling the tools the owner granted its project."""

import json
from typing import Any
from uuid import uuid4

import pytest

from app.agents import AgentOutputError, ResearcherAgent
from app.config import Settings
from app.context import ProjectContext
from app.events import InvocationRecorder
from app.gateway.client import Completion, Usage
from app.mcp import review as mcp_review
from app.mcp.calling import McpTools, Reachable
from app.mcp.client import ToolResult
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk, PolicyRefused
from app.tools.web import SearchResult, StaticSearchTool


def config() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/x",
    )


def context() -> ProjectContext:
    return ProjectContext(organization="Northwind", project_id=uuid4(), project_name="Wwft")


class Gateway:
    """Returns each content in turn and keeps every prompt it was sent."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.prompts: list[str] = []

    async def complete(
        self,
        alias: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        schema: Any = None,
    ) -> Completion:
        self.prompts.append(messages[-1]["content"])
        return Completion(
            model_alias=alias,
            model="fixture",
            content=self.contents.pop(0),
            usage=Usage(prompt_tokens=100, completion_tokens=10),
            latency_seconds=0.1,
            estimated_cost=0.01,
        )


class Gate:
    def __init__(self, verdict: Verdict = Verdict.ALLOW) -> None:
        self.verdict = verdict

    def enforce(self, *args: Any, **kwargs: Any) -> Decision:
        decision = Decision(verdict=self.verdict, reason="test", policy="test")
        if self.verdict is Verdict.DENY:
            raise PolicyRefused(decision, None)
        if self.verdict is Verdict.ASK:
            raise PolicyAsk(decision, None)
        return decision


class Transport:
    def __init__(self, content: str = "Filing 2025-17: customer due diligence applies.") -> None:
        self.content = content
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, url: str, token: str | None, allow: list[str], name: str, arguments: dict
    ) -> ToolResult:
        self.calls.append((name, arguments))
        return ToolResult(content=self.content, failed=False)


def tool(name: str = "search_filings") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Search regulatory filings.",
        "schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "claims": {},
    }


def mcp(stance: str = mcp_review.ALLOWED, gate: Gate | None = None, **over: Any) -> McpTools:
    offered = tool()
    return McpTools(
        servers=[
            Reachable(
                server_id=uuid4(),
                name="Filings",
                url="https://mcp.example.com/mcp",
                token=None,
                tools=[offered],
                review=mcp_review.decide({}, offered, stance, "Alex"),
            )
        ],
        gate=gate or Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=over.get("transport") or Transport(),
    )


def choice(*calls: dict[str, Any]) -> str:
    return json.dumps({"calls": list(calls)})


CALL = {"server": "Filings", "tool": "search_filings", "arguments": {"query": "wwft cdd"}}


def report(*urls: str) -> str:
    return json.dumps(
        {
            "research_question": "q",
            "key_findings": ["f"],
            "evidence": ["e"],
            "implications": ["i"],
            "sources": [{"title": "t", "url": url, "source_type": "primary"} for url in urls],
            "confidence": "medium",
        }
    )


def researcher(gateway: Gateway, tools: McpTools | None, **over: Any) -> ResearcherAgent:
    search = StaticSearchTool([SearchResult("t", "https://example.com", "snippet")])
    return ResearcherAgent(
        gateway,
        config(),
        search,
        over.get("recorder"),
        mcp=tools,  # type: ignore[arg-type]
    )


async def test_without_offered_tools_there_is_no_extra_completion() -> None:
    gateway = Gateway([report("https://example.com")])
    await researcher(gateway, mcp(stance=mcp_review.REFUSED)).run(
        "What does the Wwft require?", context()
    )
    assert len(gateway.prompts) == 1


async def test_a_chosen_call_is_made_and_its_text_supplied() -> None:
    transport = Transport()
    gateway = Gateway([choice(CALL), report("https://example.com")])
    await researcher(gateway, mcp(transport=transport)).run(
        "What does the Wwft require?", context()
    )
    assert transport.calls == [("search_filings", {"query": "wwft cdd"})]
    assert "Filing 2025-17" in gateway.prompts[-1]


async def test_the_choice_is_shown_the_tools_and_the_question() -> None:
    gateway = Gateway([choice(), report("https://example.com")])
    await researcher(gateway, mcp()).run("What does the Wwft require?", context())
    assert "search_filings" in gateway.prompts[0]
    assert "What does the Wwft require?" in gateway.prompts[0]


async def test_a_tool_result_is_citable_by_its_address() -> None:
    gateway = Gateway([choice(CALL), report("https://example.com")])
    agent = researcher(gateway, mcp())
    await agent.run("What does the Wwft require?", context())
    [address] = [
        line.split("]")[0][1:]
        for line in gateway.prompts[-1].splitlines()
        if line.startswith("[mcp://")
    ]
    cited = Gateway([choice(CALL), report(address)])
    result = await researcher(cited, mcp()).run("What does the Wwft require?", context())
    assert str(result.sources[0].url) == address
    assert result.sources[0].accessed_on is not None


async def test_the_same_call_has_the_same_address() -> None:
    """Parallel subtasks merge their sources; one call must be one source."""
    first, second = (
        Gateway([choice(CALL), report("https://example.com")]),
        Gateway([choice(CALL), report("https://example.com")]),
    )
    await researcher(first, mcp()).run("q one", context())
    await researcher(second, mcp()).run("q two", context())

    def addresses(prompt: str) -> list[str]:
        return [line for line in prompt.splitlines() if line.startswith("[mcp://")]

    assert addresses(first.prompts[-1]) == addresses(second.prompts[-1])


async def test_an_uncalled_tool_address_cannot_be_cited() -> None:
    gateway = Gateway([choice(), *[report("mcp://filings/search-filings/deadbeef")] * 5])
    with pytest.raises(AgentOutputError, match="unverified_source"):
        await researcher(gateway, mcp()).run("q", context())


async def test_an_unknown_tool_is_skipped_not_fatal() -> None:
    transport = Transport()
    unknown = {"server": "Filings", "tool": "delete_everything", "arguments": {}}
    gateway = Gateway([choice(unknown, CALL), report("https://example.com")])
    await researcher(gateway, mcp(transport=transport)).run("q", context())
    assert [name for name, _ in transport.calls] == ["search_filings"]


async def test_a_malformed_choice_means_no_calls() -> None:
    transport = Transport()
    gateway = Gateway(["I would call the filings tool", report("https://example.com")])
    await researcher(gateway, mcp(transport=transport)).run("q", context())
    assert transport.calls == []


async def test_calls_are_capped() -> None:
    transport = Transport()
    many = [{**CALL, "arguments": {"query": f"q{n}"}} for n in range(10)]
    gateway = Gateway([choice(*many), report("https://example.com")])
    await researcher(gateway, mcp(transport=transport)).run("q", context())
    assert len(transport.calls) == 3


async def test_a_policy_refusal_skips_the_call_and_research_continues() -> None:
    transport = Transport()
    gateway = Gateway([choice(CALL), report("https://example.com")])
    tools = mcp(gate=Gate(Verdict.DENY), transport=transport)
    result = await researcher(gateway, tools).run("q", context())
    assert transport.calls == []
    assert result.sources


async def test_the_choice_is_billed() -> None:
    recorder = InvocationRecorder()
    gateway = Gateway([choice(), report("https://example.com")])
    await researcher(gateway, mcp(), recorder=recorder).run("q", context())
    assert sum(event.prompt_tokens or 0 for event in recorder.events) == 200


async def test_a_policy_ask_pauses_rather_than_failing_the_step() -> None:
    gateway = Gateway([choice(CALL), report("https://example.com")])
    with pytest.raises(PolicyAsk):
        await researcher(gateway, mcp(gate=Gate(Verdict.ASK))).run("q", context())


async def test_a_tool_not_offered_is_never_called_even_if_the_server_has_it() -> None:
    """The choice is held to the offered set, not to what a server would accept."""
    transport = Transport()
    refused = {"server": "Filings", "tool": "search_filings", "arguments": {}}
    gateway = Gateway([choice(refused), report("https://example.com")])
    # Refused by the owner: not offered, so the choice cannot reach it.
    await researcher(gateway, mcp(stance=mcp_review.REFUSED, transport=transport)).run(
        "q", context()
    )
    assert transport.calls == []


async def test_a_paid_call_past_the_cost_ceiling_stops_the_step_as_a_budget() -> None:
    """N12: not a failed tool. The code is the budget's, so the task pauses and
    asks rather than failing; nothing is sent."""
    from app.budget import Budget, RunLedger
    from app.mcp.metering import ToolMeter

    offered = tool()
    transport = Transport()
    tools = McpTools(
        servers=[
            Reachable(
                uuid4(),
                "Filings",
                "https://mcp.example.com/mcp",
                None,
                [offered],
                mcp_review.decide({}, offered, mcp_review.ALLOWED, "Alex", price_usd=2.0),
            )
        ],
        gate=Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=transport,  # type: ignore[arg-type]
        meter=ToolMeter(
            RunLedger(Budget(max_tokens=100_000, max_cost_usd=1.0), 1000),
            lambda event: None,
            lambda: None,
            run_id=None,
            task_id=None,
            project_id=uuid4(),
        ),
    )
    gateway = Gateway([choice(CALL), report("https://example.com")])
    with pytest.raises(AgentOutputError) as stopped:
        await researcher(gateway, tools).run("q", context())
    assert stopped.value.code == "budget_exhausted:cost"
    assert transport.calls == []
    assert "paid: up to $2.00 per call" in gateway.prompts[0]
