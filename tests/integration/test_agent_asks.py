"""A coding agent's permission question goes to the owner."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.agent_asks import AgentAsks
from app.db.models import Run
from app.rooms import waiting_where

pytestmark = pytest.mark.integration
OPTIONS = [
    {"id": "yes", "name": "Allow once", "kind": "allow_once"},
    {"id": "no", "name": "Reject", "kind": "reject_once"},
]


async def a_run(client: Any) -> UUID:  # noqa: F811
    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()
    return UUID(run["id"])


async def test_the_question_waits_for_the_owners_answer_through_the_api(client: Any) -> None:  # noqa: F811
    run_id = await a_run(client)
    factory = client.app_state.session_factory
    asks = AgentAsks(factory, client.app_state.repository.add_event, poll_seconds=0.05)

    asking = asyncio.create_task(asks.ask(run_id, "DeepSeek Harness", "Run npm install", OPTIONS))
    await asyncio.sleep(0.3)
    with factory() as session:
        [question] = [q for q in waiting_where(session, Run.id == run_id) if q["kind"] == "agent"]
    assert question["reason"] == "Run npm install"
    assert [o["id"] for o in question["options"]] == ["yes", "no"]

    wrong = await client.post(
        f"/runs/{run_id}/agent-asks/{question['ask_id']}", json={"option": "maybe"}
    )
    assert (wrong.status_code, wrong.json()["detail"]) == (409, "option_unknown")
    said = await client.post(
        f"/runs/{run_id}/agent-asks/{question['ask_id']}", json={"option": "yes"}
    )
    assert said.status_code == 200
    assert await asyncio.wait_for(asking, 5) == "yes"
    with factory() as session:
        assert not [q for q in waiting_where(session, Run.id == run_id) if q["kind"] == "agent"]
    late = await client.post(
        f"/runs/{run_id}/agent-asks/{question['ask_id']}", json={"option": "no"}
    )
    assert (late.status_code, late.json()["detail"]) == (409, "ask_not_open")


async def test_an_unanswered_question_is_refused_at_its_deadline(client: Any) -> None:  # noqa: F811
    run_id = await a_run(client)
    asks = AgentAsks(
        client.app_state.session_factory,
        client.app_state.repository.add_event,
        wait=timedelta(seconds=0.4),
        poll_seconds=0.05,
    )

    assert await asks.ask(run_id, "DeepSeek Harness", "Run npm install", OPTIONS) is None

    events = [
        e for e in client.app_state.repository.events(run_id) if e["kind"].startswith("agent_")
    ]
    assert [e["kind"] for e in events] == ["agent_permission_ask", "agent_permission_settled"]
    assert events[-1]["payload"]["outcome"] == "refused_unanswered"
