"""What the owner decided about a tool, and what a server cannot decide for them."""

from uuid import uuid4

import pytest

from app.mcp.review import (
    ALLOWED,
    CHANGED,
    NEEDS_APPROVAL,
    REFUSED,
    UNREVIEWED,
    callable_tools,
    decide,
    needs_approval,
    outstanding,
    stance_of,
)


def tool(name: str = "search", description: str = "Search.", **over: object) -> dict:
    return {"name": name, "description": description, "schema": {}, "claims": {}, **over}


def test_nothing_is_callable_until_the_owner_has_looked() -> None:
    """A server offering a tool is not the office agreeing to call it."""
    offered = [tool("search"), tool("delete_all", "Danger.")]
    assert callable_tools(offered, {}) == []
    assert stance_of({}, offered[0]) == UNREVIEWED
    assert outstanding(offered, {}) == 2


def test_a_server_calling_itself_read_only_decides_nothing() -> None:
    """The specification: a client MUST treat annotations as untrusted."""
    claimed = tool("delete_all", "Danger.", claims={"readOnlyHint": True})
    assert callable_tools([claimed], {}) == []
    assert stance_of({}, claimed) == UNREVIEWED


def test_the_owner_decides_per_tool() -> None:
    offered = [tool("search"), tool("delete_all", "Danger.")]
    review = decide({}, offered[0], ALLOWED, "Alex")
    review = decide(review, offered[1], REFUSED, "Alex")

    assert stance_of(review, offered[0]) == ALLOWED
    assert stance_of(review, offered[1]) == REFUSED
    assert [row["name"] for row in callable_tools(offered, review)] == ["search"]
    assert outstanding(offered, review) == 0


def test_a_tool_that_changes_the_world_is_proposed_not_refused() -> None:
    """Needing approval is not the same as being forbidden."""
    writing = tool("file_issue", "Open an issue.")
    review = decide({}, writing, NEEDS_APPROVAL, "Alex")

    assert needs_approval(writing, review) is True
    # It is offered, because proposing it is how the owner gets asked.
    assert callable_tools([writing], review) == [writing]


def test_a_decision_does_not_survive_the_tool_changing() -> None:
    """ "I read this and it was fine" is about a thing, not about a name."""
    before = tool("search", "Search a public filing index.")
    review = decide({}, before, ALLOWED, "Alex")
    assert stance_of(review, before) == ALLOWED

    after = tool("search", "Search, and post to the owner's account.")
    assert stance_of(review, after) == CHANGED
    assert callable_tools([after], review) == []
    assert outstanding([after], review) == 1


def test_a_server_changing_only_its_own_claim_is_not_a_change() -> None:
    """The office never believed the hint, so it cannot be changed by it."""
    before = tool("search", claims={})
    review = decide({}, before, ALLOWED, "Alex")
    after = tool("search", claims={"readOnlyHint": True, "destructiveHint": False})
    assert stance_of(review, after) == ALLOWED


def test_an_unknown_stance_is_refused_rather_than_stored() -> None:
    with pytest.raises(ValueError, match="unknown stance"):
        decide({}, tool(), "probably_fine", "Alex")


def test_a_review_written_by_something_else_is_not_trusted() -> None:
    """A malformed record reads as unreviewed, never as allowed."""
    for junk in ({"search": "allowed"}, {"search": {}}, {"search": {"stance": "yes"}}):
        assert stance_of(junk, tool("search")) == UNREVIEWED


# Section 18 outranks a tool review.


def _tool() -> dict:
    return {"name": "send", "description": "Send a message.", "schema": {}, "claims": {}}


def test_a_tool_tagged_with_a_denied_action_is_refused_whatever_its_stance() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", acts_as=["send_external_email"])
    assert r.stance_of(decided, _tool()) == r.DENIED_BY_POLICY
    assert r.callable_tools([_tool()], decided) == []


def test_a_tool_tagged_with_an_approval_action_is_at_most_needs_approval() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", acts_as=["modify_code"])
    assert r.stance_of(decided, _tool()) == r.NEEDS_APPROVAL
    assert r.needs_approval(_tool(), decided)


def test_a_tool_tagged_with_an_automatic_action_keeps_the_owners_stance() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", acts_as=["web_search"])
    assert r.stance_of(decided, _tool()) == r.ALLOWED


def test_an_unknown_action_cannot_be_recorded() -> None:
    from app.mcp import review as r

    with pytest.raises(ValueError):
        r.decide({}, _tool(), r.ALLOWED, "Alex", acts_as=["launch_rockets"])


def test_a_malformed_tag_reads_as_unreviewed() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex")
    decided["send"]["acts_as"] = "send_external_email"
    assert r.stance_of(decided, _tool()) == r.UNREVIEWED
    decided["send"]["acts_as"] = ["launch_rockets"]
    assert r.stance_of(decided, _tool()) == r.UNREVIEWED


def test_the_view_shows_what_the_owner_chose_and_what_applies() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", acts_as=["delete_external_data"])
    [row] = r.reviewed([_tool()], decided)
    assert row["stance"] == r.DENIED_BY_POLICY
    assert row["chosen"] == r.ALLOWED
    assert row["acts_as"] == ["delete_external_data"]


async def test_a_denied_tool_is_never_sent() -> None:
    from app.mcp import review as r
    from app.mcp.calling import McpTools, Reachable, ToolRefused
    from app.mcp.client import ToolResult

    sent: list[str] = []

    class Transport:
        async def call(self, *args: object) -> ToolResult:
            sent.append("called")
            return ToolResult(content="", failed=False)

    class Gate:
        def enforce(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("a denied tool is not a policy question")

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", acts_as=["send_external_email"])
    tools = McpTools(
        [Reachable(uuid4(), "Mail", "https://mcp.example.com/mcp", None, [_tool()], decided)],
        Gate(),
        None,
        None,
        [],
        client=Transport(),  # type: ignore[arg-type]
    )
    assert tools.offered() == []
    with pytest.raises(ToolRefused) as refused:
        await tools.call("Mail", "send", {})
    assert refused.value.reason == "tool_denied_by_policy"
    assert sent == []


# A price on a reviewed tool.


def test_a_decision_can_carry_the_most_one_call_costs() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", price_usd=0.25)
    assert r.price_of(decided, _tool()) == 0.25
    assert r.reviewed([_tool()], decided)[0]["price_usd"] == 0.25


def test_a_tool_with_no_price_costs_nothing_the_office_can_see() -> None:
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex")
    assert r.price_of(decided, _tool()) is None
    assert r.price_of({}, _tool()) is None
    assert r.reviewed([_tool()], decided)[0]["price_usd"] is None


@pytest.mark.parametrize("price", [-1.0, 0.0, 1001.0, float("nan"), float("inf")])
def test_a_price_that_is_not_a_plausible_charge_is_refused(price: float) -> None:
    from app.mcp import review as r

    with pytest.raises(ValueError):
        r.decide({}, _tool(), r.ALLOWED, "Alex", price_usd=price)


def test_a_changed_tool_has_no_price_until_it_is_reviewed_again() -> None:
    """The price was set for the tool as the owner read it."""
    from app.mcp import review as r

    decided = r.decide({}, _tool(), r.ALLOWED, "Alex", price_usd=0.5)
    changed = {**_tool(), "description": "Send a message, and charge for it."}
    assert r.price_of(decided, changed) is None
