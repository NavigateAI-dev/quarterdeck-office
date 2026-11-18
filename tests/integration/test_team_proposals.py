"""The lead's proposal of a full team run, started from the task with one press."""

from uuid import UUID, uuid4

import httpx
import pytest

from app.db.models import RunEvent

pytestmark = pytest.mark.integration

WHY = "It needs code written and run, which only the full team does."


async def lead_task_proposing(client: httpx.AsyncClient, objective: str | None) -> str:
    """A lead task that proposed a team run (or, with None, did not)."""
    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run_id = (
        await client.post("/runs", json={"project_id": project_id, "objective": "Slack check"})
    ).json()["id"]
    if objective is not None:
        state = client.app_state  # type: ignore[attr-defined]
        with state.session_factory() as session, session.begin():
            payload = {"objective": objective, "why": WHY}
            session.add(
                RunEvent(run_id=UUID(run_id), kind="team_run_proposed", payload_json=payload)
            )
    return str(run_id)


async def test_a_task_shows_the_lead_s_proposal_until_it_is_started(
    client: httpx.AsyncClient,
) -> None:
    run_id = await lead_task_proposing(client, "Build the Slack self-check tool")

    shown = (await client.get(f"/runs/{run_id}")).json()["team_proposal"]

    assert shown == {
        "objective": "Build the Slack self-check tool",
        "why": WHY,
        "started_run_id": None,
    }


async def test_the_press_starts_the_full_team_on_it_building_on_the_task(
    client: httpx.AsyncClient,
) -> None:
    run_id = await lead_task_proposing(client, "Build the Slack self-check tool")

    started = await client.post(f"/runs/{run_id}/team-run")

    assert started.status_code == 202, started.text
    team = started.json()
    assert team["workflow_name"] == "v0_analysis"
    assert team["continues_run_id"] == run_id
    team_task = (await client.get(f"/runs/{team['id']}")).json()
    assert team_task["objective"] == "Build the Slack self-check tool"
    after = (await client.get(f"/runs/{run_id}")).json()["team_proposal"]
    assert after["started_run_id"] == team["id"]


async def test_a_second_press_starts_nothing_more(client: httpx.AsyncClient) -> None:
    run_id = await lead_task_proposing(client, "Build the Slack self-check tool")
    await client.post(f"/runs/{run_id}/team-run")

    again = await client.post(f"/runs/{run_id}/team-run")

    assert again.status_code == 409
    assert again.json()["detail"] == "already_started"


async def test_a_task_with_no_proposal_has_nothing_to_start(client: httpx.AsyncClient) -> None:
    run_id = await lead_task_proposing(client, None)

    assert (await client.get(f"/runs/{run_id}")).json()["team_proposal"] is None
    refused = await client.post(f"/runs/{run_id}/team-run")
    assert (refused.status_code, refused.json()["detail"]) == (404, "no_team_proposal")
    assert (await client.post(f"/runs/{uuid4()}/team-run")).status_code == 404
