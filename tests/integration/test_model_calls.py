"""Every model call a run makes, kept so it can be replayed."""

from typing import Any

import pytest
from sqlalchemy import func, select

from app.agent_routing import keep, members
from app.db.models import ModelPayload
from app.gateway.client import GatewayError
from app.lead.run import execute_lead_run
from app.model_calls import NOT_RECORDED, KeptCompleter, ModelCalls, ReplayCompleter
from app.orchestrator.graph import LEAD_WORKFLOW
from app.orchestrator.state import RunStatus
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture
from tests.unit.test_lead_run import Model, proposing, said, team

pytestmark = pytest.mark.integration

SYSTEM = {"role": "system", "content": "You are the lead. " * 50}


async def test_each_call_is_kept_and_each_message_once(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "Q").run_id
    calls = ModelCalls(factory)
    kept = KeptCompleter(Model(said("First."), said("Second.")), calls, run_id, "chief")

    first = [SYSTEM, {"role": "user", "content": "Rain?"}]
    await kept.complete("chief", first)
    await kept.complete("chief", [*first, {"role": "assistant", "content": "First."}])

    listed = calls.of_run(run_id)
    assert [(c["role"], c["status"], c["messages"]) for c in listed] == [
        ("chief", "ok", 2),
        ("chief", "ok", 3),
    ]
    whole = calls.call(listed[1]["id"])
    assert whole is not None
    assert whole["messages"][-1] == {"role": "assistant", "content": "First."}
    assert whole["response"]["content"] == "Second."
    with factory() as session:
        # Three messages and two answers: the system prompt is kept once.
        assert session.scalar(select(func.count()).select_from(ModelPayload)) == 5


async def test_a_failed_call_is_kept_with_its_reason(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    run_id = repository.create(project_id, "Q").run_id
    calls = ModelCalls(factory)
    kept = KeptCompleter(Model(GatewayError("provider_timeout")), calls, run_id, "chief")

    with pytest.raises(GatewayError):
        await kept.complete("chief", [{"role": "user", "content": "Rain?"}])

    [call] = calls.of_run(run_id)
    assert (call["status"], call["model"]) == ("provider_timeout", None)


async def test_a_lead_run_plays_again_from_its_kept_calls_with_no_model(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    calls = ModelCalls(factory)
    first = repository.create(project_id, "Rain in Lyon?", workflow=LEAD_WORKFLOW)
    agents = keep(team(Model(proposing(), said("No rain today."))), calls, first.run_id)
    asked = await execute_lead_run(
        first,
        repository,
        agents,
        "Rain in Lyon?",
        {"project_name": "Pilot"},
        work=[],
        worker_id="w1",
    )

    # The same run again, answered from what was kept: no gateway, no key.
    replay = ReplayCompleter(calls.recorded(first.run_id))
    again = repository.create(project_id, "Rain in Lyon?", workflow=LEAD_WORKFLOW)
    replayed = team(Model())
    for agent in members(replayed):
        agent.gateway = replay
    played = await execute_lead_run(
        again,
        repository,
        replayed,
        "Rain in Lyon?",
        {"project_name": "Pilot"},
        work=[],
        worker_id="w1",
    )

    assert played["status"] == asked["status"] == RunStatus.COMPLETED
    assert played["synthesis"] == asked["synthesis"]
    assert len(calls.of_run(first.run_id)) == 2


async def test_a_request_that_was_not_made_is_refused_not_answered() -> None:
    replay = ReplayCompleter({})
    with pytest.raises(GatewayError) as refused:
        await replay.complete("chief", [{"role": "user", "content": "Something new"}])
    assert refused.value.code == NOT_RECORDED


async def test_the_owner_reads_a_runs_calls_through_the_api(client: Any) -> None:  # noqa: F811
    from uuid import UUID

    from app.model_calls import request_of

    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()
    shaped, bodies = request_of("chief", [{"role": "user", "content": "Rain?"}], None, None, None)
    made = ModelCalls(client.app_state.session_factory).keep(
        UUID(run["id"]), "chief", shaped, bodies, said("No rain."), "ok", 0.4
    )

    listed = (await client.get(f"/runs/{run['id']}/model-calls")).json()
    assert [(c["id"], c["role"], c["messages"]) for c in listed] == [(made, "chief", 1)]
    whole = (await client.get(f"/model-calls/{made}")).json()
    assert whole["messages"] == [{"role": "user", "content": "Rain?"}]
    assert whole["response"]["content"] == "No rain."
    assert (await client.get("/model-calls/999999")).status_code == 404
    assert (
        await client.get(f"/model-calls/{made}", headers={"Authorization": ""})
    ).status_code == 401
