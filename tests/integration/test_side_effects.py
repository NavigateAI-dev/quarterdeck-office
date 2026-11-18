"""Writes that leave the office, checkpointed before they run.

A run picked up again after a restart replays its steps. A call it already made
is not sent again; one the office stopped in the middle of is "unknown" until
the owner, who can look at the third party, settles it.
"""

from typing import Any
from uuid import uuid4

import pytest

from app.mcp import review as mcp_review
from app.side_effects import DONE, FAILED, INTENDED, UNKNOWN, SideEffects, SideEffectSettled
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture
from tests.unit.test_mcp_calling import Gate, Transport, reachable, tool

pytestmark = pytest.mark.integration


def tools(run_id: Any, ledger: SideEffects, transport: Transport, events: list[Any]) -> Any:
    from app.mcp.calling import McpTools

    review = mcp_review.decide({}, tool(), mcp_review.ALLOWED, "Alex")
    return McpTools(
        servers=[reachable(review)],
        gate=Gate(),
        run_id=run_id,
        project_id=uuid4(),
        allow_list=[],
        client=transport,  # type: ignore[arg-type]
        writes=ledger.for_run(run_id, lambda kind, data: events.append((kind, data))),
    )


async def test_a_replayed_run_does_not_call_again_what_it_already_called(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "File the report").run_id
    ledger = SideEffects(factory)
    first, events = Transport(), []

    await tools(run_id, ledger, first, events).call("Filings", "search", {"q": "wwft"})
    # The office restarts; the run replays its step and makes the same call.
    again = Transport()
    said = await tools(run_id, ledger, again, events).call("Filings", "search", {"q": "wwft"})

    assert len(first.calls) == 1 and again.calls == []
    assert "already made" in said
    assert [row["status"] for row in ledger.of_run(run_id)] == [DONE]


async def test_two_identical_calls_in_one_attempt_stay_two(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "Check twice").run_id
    ledger = SideEffects(factory)
    transport = Transport()
    calls = tools(run_id, ledger, transport, [])

    await calls.call("Filings", "search", {"q": "x"})
    await calls.call("Filings", "search", {"q": "x"})

    assert len(transport.calls) == 2
    assert len(ledger.of_run(run_id)) == 2


async def test_a_call_cut_off_by_a_restart_is_unknown_and_the_owner_is_told(
    environment: Any,  # noqa: F811
) -> None:
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "File the report").run_id
    ledger = SideEffects(factory)
    # Written down, sent, and then the office stopped before it heard back.
    ledger.for_run(run_id).intend("mcp_call", "Filings", "search", {"q": "wwft"})

    events: list[Any] = []
    transport = Transport()
    said = await tools(run_id, ledger, transport, events).call("Filings", "search", {"q": "wwft"})

    assert transport.calls == [] and "may or may not" in said
    [row] = ledger.of_run(run_id)
    assert (row["status"], row["subject"]) == (UNKNOWN, "search on Filings")
    assert events[0][0] == "side_effect_unknown"


async def test_a_call_that_failed_may_go_again(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "File the report").run_id
    ledger = SideEffects(factory)
    writes = ledger.for_run(run_id)
    row, _ = writes.intend("mcp_call", "Filings", "search", {})
    writes.settle(row, FAILED, "unreachable")

    assert ledger.for_run(run_id).intend("mcp_call", "Filings", "search", {}) == (row, INTENDED)


async def test_the_owner_settles_an_unknown_call_and_only_an_unknown_one(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "File the report").run_id
    ledger = SideEffects(factory)
    row, _ = ledger.for_run(run_id).intend("mcp_call", "Filings", "search", {})
    ledger.for_run(run_id).intend("mcp_call", "Filings", "search", {})

    settled = ledger.settle_by_owner(row, DONE, "Alex")

    assert (settled["status"], settled["settled_by"]) == (DONE, "Alex")
    with pytest.raises(SideEffectSettled):
        ledger.settle_by_owner(row, FAILED, "Alex")


async def test_the_owner_sees_and_settles_through_the_api(client: Any) -> None:  # noqa: F811
    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()
    ledger = SideEffects(client.app_state.session_factory)
    row, _ = ledger.for_run(__import__("uuid").UUID(run["id"])).intend(
        "mcp_call", "Filings", "search", {}
    )
    ledger.for_run(__import__("uuid").UUID(run["id"])).intend("mcp_call", "Filings", "search", {})

    listed = (await client.get(f"/runs/{run['id']}/side-effects")).json()
    assert [(r["id"], r["status"]) for r in listed] == [(row, UNKNOWN)]
    done = await client.post(f"/side-effects/{row}/settle", json={"status": "done"})
    assert done.status_code == 200 and done.json()["status"] == DONE
    again = await client.post(f"/side-effects/{row}/settle", json={"status": "failed"})
    assert again.status_code == 409
    missing = await client.post("/side-effects/999999/settle", json={"status": "done"})
    assert missing.status_code == 404
    refused = await client.post(f"/side-effects/{row}/settle", json={"status": "maybe"})
    assert refused.status_code == 422


async def test_the_thread_says_so_and_offers_to_settle_it(client: Any) -> None:  # noqa: F811
    from uuid import UUID

    from app.db.models import Run

    started = (await client.post("/topics", json={"text": "@Nobody-here file it"})).json()
    topic = started["topic"]["id"]
    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()
    factory = client.app_state.session_factory
    with factory() as session, session.begin():
        row = session.get(Run, UUID(run["id"]))
        assert row is not None
        row.topic_id = UUID(topic)
    ledger = SideEffects(factory)
    effect, _ = ledger.for_run(UUID(run["id"])).intend("mcp_call", "Filings", "file", {})
    repository = client.app_state.repository
    ledger.for_run(
        UUID(run["id"]), lambda kind, data: repository.add_event(UUID(run["id"]), kind, data)
    ).intend("mcp_call", "Filings", "file", {})

    feed = (await client.get(f"/topics/{topic}/feed")).json()["items"]
    [line] = [item for item in feed if item.get("effect_id")]
    assert line["effect_id"] == effect and line["waiting"] is True
    assert "file on Filings" in line["text"] and "may or may not" in line["text"]
