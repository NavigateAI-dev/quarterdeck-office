"""What stands between an agent and a third party's tool."""

from typing import Any
from uuid import uuid4

import pytest

from app.mcp import review as mcp_review
from app.mcp.calling import ApprovalRequired, McpTools, Reachable, ToolRefused
from app.mcp.client import McpError, ToolResult
from app.policies.engine import Verdict
from app.policies.store import PolicyRefused


def tool(name: str = "search", description: str = "Search.", **over: object) -> dict:
    return {"name": name, "description": description, "schema": {}, "claims": {}, **over}


class Gate:
    """A policy gate that records what it was asked, and may refuse."""

    def __init__(self, verdict: Verdict = Verdict.ALLOW) -> None:
        self.verdict = verdict
        self.asked: list[dict[str, Any]] = []

    def enforce(self, hook: Any, name: str, **facts: Any) -> Any:
        self.asked.append({"hook": hook, "name": name, **facts})
        if self.verdict is Verdict.DENY:
            raise PolicyRefused(_decision(), None)
        return _decision()


def _decision() -> Any:
    from app.policies.engine import Decision

    return Decision(verdict=Verdict.ALLOW, reason="test", policy="test")


class Transport:
    """A transport that records calls and returns whatever it was given."""

    def __init__(self, result: ToolResult | Exception | None = None) -> None:
        self.result = result or ToolResult(content="found it", failed=False)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, url: str, token: str | None, allow: list[str], name: str, arguments: dict
    ) -> ToolResult:
        self.calls.append((name, arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def reachable(review: dict[str, Any], tools: list[dict] | None = None) -> Reachable:
    return Reachable(
        server_id=uuid4(),
        name="Filings",
        url="https://mcp.example.com/mcp",
        token="sk-secret",
        tools=tools or [tool()],
        review=review,
    )


def tools_for(review: dict, gate: Gate, transport: Transport, **over: Any) -> McpTools:
    return McpTools(
        servers=[reachable(review, over.get("tools"))],
        gate=gate,
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=transport,  # type: ignore[arg-type]
    )


async def test_an_allowed_tool_is_called_and_its_text_returned() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    transport = Transport()
    got = await tools_for(review, Gate(), transport).call("Filings", "search", {"q": "wwft"})

    assert got == "found it"
    assert transport.calls == [("search", {"q": "wwft"})]


async def test_a_policy_refusal_never_reaches_the_server() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    transport = Transport()
    with pytest.raises(PolicyRefused):
        await tools_for(review, Gate(Verdict.DENY), transport).call("Filings", "search", {})
    assert transport.calls == [], "a denied call must not be sent"


async def test_an_unreviewed_tool_is_refused_before_a_policy_is_even_asked() -> None:
    """A refusal should be quiet: nothing attempted, nothing recorded."""
    gate = Gate()
    transport = Transport()
    with pytest.raises(ToolRefused) as refused:
        await tools_for({}, gate, transport).call("Filings", "search", {})

    assert refused.value.reason == "tool_unreviewed"
    assert gate.asked == [], "an unreviewed tool is not a policy question"
    assert transport.calls == []


async def test_a_tool_needing_approval_stops_even_when_the_server_calls_it_read_only() -> None:
    """The specification: a client MUST treat annotations as untrusted."""
    claiming = tool("file_issue", "Open an issue.", claims={"readOnlyHint": True})
    review = mcp_review.decide({}, claiming, mcp_review.NEEDS_APPROVAL, "Alex")
    transport = Transport()

    with pytest.raises(ApprovalRequired) as asked:
        await tools_for(review, Gate(), transport, tools=[claiming]).call(
            "Filings", "file_issue", {}
        )
    assert asked.value.tool == "file_issue"
    assert transport.calls == [], "nothing is sent before the owner decides"


async def test_a_failing_server_fails_the_call_and_not_the_run() -> None:
    """A third party being down is not a reason to throw away the work."""
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    said = await tools_for(review, Gate(), Transport(McpError("unreachable"))).call(
        "Filings", "search", {}
    )
    assert "could not be called" in said and "unreachable" in said


async def test_a_tool_reporting_its_own_failure_is_reported_not_raised() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    said = await tools_for(
        review, Gate(), Transport(ToolResult(content="stack trace", failed=True))
    ).call("Filings", "search", {})
    assert "reported a failure" in said
    assert "stack trace" not in said


async def test_a_server_that_was_not_granted_is_refused() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    with pytest.raises(ToolRefused) as refused:
        await tools_for(review, Gate(), Transport()).call("Somewhere else", "search", {})
    assert refused.value.reason == "server_not_granted"


async def test_an_agent_is_only_told_about_tools_it_could_use() -> None:
    allowed, refused = tool("search"), tool("delete_all", "Danger.")
    review = mcp_review.decide({}, allowed, mcp_review.ALLOWED, "Alex")
    review = mcp_review.decide(review, refused, mcp_review.REFUSED, "Alex")

    offered = tools_for(review, Gate(), Transport(), tools=[allowed, refused]).offered()
    assert [row["name"] for row in offered] == ["search"]
    assert offered[0]["server"] == "Filings"
    assert offered[0]["needs_approval"] is False


async def test_an_ask_pauses_the_run_rather_than_being_swallowed() -> None:
    """ASK is the office's own question, and must reach the run machinery."""
    from app.policies.store import PolicyAsk

    class Asking(Gate):
        def enforce(self, hook: Any, name: str, **facts: Any) -> Any:
            self.asked.append({"hook": hook, "name": name, **facts})
            raise PolicyAsk(_decision(), None)

    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    transport = Transport()
    with pytest.raises(PolicyAsk):
        await tools_for(review, Asking(), transport).call("Filings", "search", {})
    assert transport.calls == []


async def test_every_call_is_recorded_without_its_arguments() -> None:
    """The arguments are model output bound for a third party."""
    noted: list[tuple[str, dict[str, Any]]] = []
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    tools = McpTools(
        servers=[reachable(review)],
        gate=Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=Transport(),  # type: ignore[arg-type]
        record=lambda kind, payload: noted.append((kind, payload)),
    )
    await tools.call("Filings", "search", {"q": "something private"})

    assert noted[0][0] == "mcp_call"
    assert noted[0][1] == {
        "server": "Filings",
        "tool": "search",
        "outcome": "ok",
        "characters": len("found it"),
    }
    assert "something private" not in str(noted)


async def test_a_call_that_could_not_be_made_is_recorded_too() -> None:
    noted: list[tuple[str, dict[str, Any]]] = []
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    tools = McpTools(
        servers=[reachable(review)],
        gate=Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=Transport(McpError("unauthorised")),  # type: ignore[arg-type]
        record=lambda kind, payload: noted.append((kind, payload)),
    )
    await tools.call("Filings", "search", {})
    assert noted[0][1]["outcome"] == "unauthorised"


class AskingGate(Gate):
    """A gate that can put a question to the owner, who may already have said yes."""

    def __init__(self, allowed: bool = False) -> None:
        super().__init__()
        self.allowed = allowed
        self.questions: list[dict[str, Any]] = []

    def ask_owner(self, subject: str, reason: str, target: str, **facts: Any) -> None:
        from app.policies.store import PolicyAsk

        self.questions.append({"subject": subject, "reason": reason, **facts})
        if not self.allowed:
            raise PolicyAsk(_decision(), 7)


async def test_a_tool_needing_approval_asks_the_owner_and_sends_nothing() -> None:
    from app.policies.store import PolicyAsk

    review = mcp_review.decide({}, tool("file_issue"), mcp_review.NEEDS_APPROVAL, "Alex")
    gate, transport = AskingGate(), Transport()
    with pytest.raises(PolicyAsk):
        await tools_for(review, gate, transport, tools=[tool("file_issue")]).call(
            "Filings", "file_issue", {"title": "Wwft gap"}
        )
    [question] = gate.questions
    assert question["subject"].startswith("mcp:Filings:file_issue#")
    # The owner decides on what would be sent, so they are shown it.
    assert "Wwft gap" in question["reason"]
    assert transport.calls == []


async def test_once_the_owner_allowed_it_the_call_goes_through_the_policies() -> None:
    review = mcp_review.decide({}, tool("file_issue"), mcp_review.NEEDS_APPROVAL, "Alex")
    gate, transport = AskingGate(allowed=True), Transport()
    await tools_for(review, gate, transport, tools=[tool("file_issue")]).call(
        "Filings", "file_issue", {}
    )
    assert transport.calls == [("file_issue", {})]
    assert len(gate.asked) == 1, "the office's own policies still apply"


class GrantingGate(Gate):
    """A gate that remembers which subjects the owner allowed, as the store does."""

    def __init__(self) -> None:
        super().__init__()
        self.allowed: set[str] = set()

    def ask_owner(self, subject: str, reason: str, target: str, **facts: Any) -> None:
        from app.policies.store import PolicyAsk

        if subject not in self.allowed:
            raise PolicyAsk(_decision(), 7)


def _approval_tools(servers: list[Reachable], gate: Gate, transport: Transport) -> McpTools:
    return McpTools(
        servers=servers,
        gate=gate,
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=transport,  # type: ignore[arg-type]
    )


def _approval_server(name: str, *names: str) -> Reachable:
    listed = [tool(n) for n in names]
    review: dict[str, Any] = {}
    for row in listed:
        review = mcp_review.decide(review, row, mcp_review.NEEDS_APPROVAL, "Alex")
    return Reachable(uuid4(), name, "https://mcp.example.com/mcp", None, listed, review)


async def _subject_of(tools: McpTools, server: str, name: str) -> str:
    from app.policies.store import PolicyAsk

    asked: list[str] = []
    gate = tools.gate
    original = gate.ask_owner

    def spy(subject: str, *args: Any, **facts: Any) -> None:
        asked.append(subject)
        original(subject, *args, **facts)

    gate.ask_owner = spy  # type: ignore[method-assign]
    try:
        await tools.call(server, name, {})
    except PolicyAsk:
        pass
    finally:
        gate.ask_owner = original  # type: ignore[method-assign]
    return asked[0]


async def test_approving_one_tool_does_not_approve_another_on_a_long_named_server() -> None:
    from app.policies.store import PolicyAsk

    server = _approval_server("S" * 60, "read_draft", "delete_account")
    gate, transport = GrantingGate(), Transport()
    tools = _approval_tools([server], gate, transport)
    gate.allowed.add(await _subject_of(tools, server.name, "read_draft"))
    await tools.call(server.name, "read_draft", {})
    with pytest.raises(PolicyAsk):
        await tools.call(server.name, "delete_account", {})
    assert [name for name, _ in transport.calls] == ["read_draft"]


async def test_approving_a_tool_does_not_approve_one_whose_name_splits_differently() -> None:
    from app.policies.store import PolicyAsk

    first, second = _approval_server("a:b", "c"), _approval_server("a", "b:c")
    gate, transport = GrantingGate(), Transport()
    tools = _approval_tools([first, second], gate, transport)
    gate.allowed.add(await _subject_of(tools, "a:b", "c"))
    with pytest.raises(PolicyAsk):
        await tools.call("a", "b:c", {})
    assert transport.calls == []


async def test_two_servers_with_the_same_name_do_not_share_an_approval() -> None:
    first, second = (
        _approval_server("Filings", "file_issue"),
        _approval_server("Filings", "file_issue"),
    )
    gate = GrantingGate()
    one = await _subject_of(_approval_tools([first], gate, Transport()), "Filings", "file_issue")
    two = await _subject_of(_approval_tools([second], gate, Transport()), "Filings", "file_issue")
    assert one != two


async def test_an_approval_subject_fits_the_policy_record() -> None:
    from app.mcp.calling import SUBJECT_LENGTH

    server = _approval_server("S" * 128, "t" * 200)
    subject = await _subject_of(
        _approval_tools([server], GrantingGate(), Transport()), server.name, "t" * 200
    )
    assert len(subject) <= SUBJECT_LENGTH


class Writes:
    """A ledger that records what it was told, answering as scripted."""

    def __init__(self, answer: str = "intended") -> None:
        self.answer = answer
        self.intended: list[tuple[str, str, str, dict[str, Any]]] = []
        self.settled: list[tuple[int, str, str]] = []

    def intend(
        self, kind: str, server: str, tool: str, arguments: dict[str, Any]
    ) -> tuple[int, str]:
        self.intended.append((kind, server, tool, arguments))
        return 1, self.answer

    def settle(self, row_id: int, status: str, outcome: str) -> None:
        self.settled.append((row_id, status, outcome))


def written(review: dict, transport: Transport, writes: Writes) -> McpTools:
    return McpTools(
        servers=[reachable(review)],
        gate=Gate(),
        run_id=uuid4(),
        project_id=uuid4(),
        allow_list=[],
        client=transport,  # type: ignore[arg-type]
        writes=writes,  # type: ignore[arg-type]
    )


async def test_a_call_is_written_down_before_it_is_sent_and_settled_after() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    writes = Writes()

    got = await written(review, Transport(), writes).call("Filings", "search", {"q": "wwft"})

    assert got == "found it"
    assert writes.intended == [("mcp_call", "Filings", "search", {"q": "wwft"})]
    assert writes.settled == [(1, "done", "ok")]


@pytest.mark.parametrize(
    ("answer", "said"), [("done", "already made"), ("unknown", "may or may not")]
)
async def test_a_call_made_before_a_restart_is_not_sent_again(answer: str, said: str) -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    transport = Transport()
    writes = Writes(answer)

    got = await written(review, transport, writes).call("Filings", "search", {"q": "wwft"})

    assert transport.calls == [], "the third party is not called again"
    assert said in got and writes.settled == []


@pytest.mark.parametrize(
    ("code", "settled"),
    [("no_answer", "unknown"), ("unreachable", "failed"), ("http_500", "failed")],
)
async def test_a_call_sent_with_no_answer_is_unknown_and_one_not_sent_failed(
    code: str, settled: str
) -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    writes = Writes()

    await written(review, Transport(McpError(code)), writes).call("Filings", "search", {})

    assert writes.settled == [(1, settled, code)]


async def test_a_tool_that_reports_a_failure_may_be_tried_again() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    writes = Writes()

    await written(review, Transport(ToolResult(content="", failed=True)), writes).call(
        "Filings", "search", {}
    )

    assert writes.settled == [(1, "failed", "tool_failed")]


async def test_a_refused_call_is_never_written_down() -> None:
    review = mcp_review.decide({}, tool(), mcp_review.REFUSED, "Alex")
    writes = Writes()

    with pytest.raises(ToolRefused):
        await written(review, Transport(), writes).call("Filings", "search", {})

    assert writes.intended == []
