"""The owner's verdict on the lead's answers, and the numbers."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update

from app.db.models import RoomMessage, Run, UsageEvent
from tests.integration.test_topic_settings import thread


async def reply_in(client: httpx.AsyncClient, topic: str) -> tuple[int, int]:  # noqa: F811
    """The owner's last message and the agent's reply to it."""
    await client.post(f"/topics/{topic}/messages", json={"text": "@Cato what is the VAT rate?"})
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session:
        rows = session.scalars(
            select(RoomMessage).where(RoomMessage.topic_id == UUID(topic)).order_by(RoomMessage.id)
        ).all()
    answer = next(r for r in reversed(rows) if r.author_kind == "agent")
    assert answer.reply_to is not None
    return int(answer.reply_to), int(answer.id)


async def test_a_reply_takes_a_verdict_and_the_latest_wins(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic, _ = await thread(client, monkeypatch)
    asked, answer = await reply_in(client, topic)
    up = await client.put(f"/topics/{topic}/messages/{answer}/verdict", json={"verdict": "up"})
    assert up.status_code == 200 and up.json()["verdict"] == "up"
    down = await client.put(
        f"/topics/{topic}/messages/{answer}/verdict",
        json={"verdict": "down", "note": "No source for the rate"},
    )
    assert down.json() == {"verdict": "down", "note": "No source for the rate", "redo_run_id": None}
    feed = (await client.get(f"/topics/{topic}/feed?after_event=0&after_message=0")).json()
    shown = next(i for i in feed["items"] if i["id"] == f"m:{answer}")
    assert shown["verdict"]["verdict"] == "down"
    mine = await client.put(f"/topics/{topic}/messages/{asked}/verdict", json={"verdict": "up"})
    assert mine.status_code == 409 and mine.json()["detail"] == "not_an_answer"
    wrong = await client.put(f"/topics/{topic}/messages/{answer}/verdict", json={"verdict": "meh"})
    assert wrong.status_code == 422


async def test_redo_with_the_full_team_takes_up_the_same_question(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic, _ = await thread(client, monkeypatch)
    _, answer = await reply_in(client, topic)
    redone = await client.post(f"/topics/{topic}/messages/{answer}/redo")
    assert redone.status_code == 201, redone.text
    view = redone.json()
    assert view["verdict"] == "down" and view["redo_run_id"]
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session:
        run = session.get(Run, UUID(view["redo_run_id"]))
        assert run is not None and run.topic_id == UUID(topic)
        assert run.workflow_name != "lead", "the full team, not the lead again"
    again = await client.post(f"/topics/{topic}/messages/{answer}/redo")
    assert again.status_code == 409 and again.json()["detail"] == "already_redone"


async def test_a_task_answer_takes_a_verdict_once_it_is_answered(
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Verdicts"})).json()["id"]
    dispatch = state.repository.create(UUID(project), "Dutch VAT rate")
    early = await client.put(f"/runs/{dispatch.run_id}/verdict", json={"verdict": "up"})
    assert early.status_code == 409 and early.json()["detail"] == "not_answered_yet"
    with state.session_factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == dispatch.run_id).values(status="COMPLETED"))
    judged = await client.put(f"/runs/{dispatch.run_id}/verdict", json={"verdict": "up"})
    assert judged.status_code == 200
    got = (await client.get(f"/runs/{dispatch.run_id}/verdict")).json()
    assert got["verdict"]["verdict"] == "up"


async def test_the_report_sets_the_lead_beside_the_team(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Quality"})).json()["id"]
    now = datetime.now(UTC)

    def finished(workflow: str, calls: int, cost: float, seconds: int) -> Any:
        dispatch = state.repository.create(UUID(project), f"{workflow} task")
        with state.session_factory() as session, session.begin():
            session.execute(
                update(Run)
                .where(Run.id == dispatch.run_id)
                .values(
                    workflow_name=workflow,
                    status="COMPLETED",
                    started_at=now - timedelta(seconds=seconds),
                    completed_at=now,
                )
            )
            for _ in range(calls):
                session.add(
                    UsageEvent(
                        run_id=dispatch.run_id,
                        task_id=dispatch.task_id,
                        project_id=dispatch.project_id,
                        role="chief",
                        provider="x",
                        model_alias="a",
                        status="ok",
                        prompt_tokens=10,
                        completion_tokens=5,
                        estimated_cost=cost / calls,
                        attempts=1,
                        latency_seconds=1.0,
                        occurred_at=now,
                    )
                )
        return dispatch

    finished("lead", 2, 0.02, 10)
    finished("v0_analysis", 16, 0.35, 65)
    topic, _ = await thread(client, monkeypatch)
    _, answer = await reply_in(client, topic)
    await client.put(f"/topics/{topic}/messages/{answer}/verdict", json={"verdict": "up"})
    report = (await client.get("/quality/lead?days=30")).json()
    lead, team = report["paths"]["lead"], report["paths"]["team"]
    assert (lead["median_calls"], lead["median_seconds"]) == (2, 10)
    assert lead["median_cost_usd"] == pytest.approx(0.02)
    assert team["median_calls"] == 16 and team["median_seconds"] == 65
    assert report["verdicts"]["up"] >= 1 and report["verdicts"]["on_replies"] >= 1
    assert report["replies"]["count"] >= 1
    assert "reworked_within_a_day" in report


async def test_start_in_a_discussion_goes_to_the_lead_unless_the_team_is_asked(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner, 23 Oct: Start takes the lead (whatever the project's default
    deliverable). On 27 Oct the setting "the full team answers" was retired:
    the team is asked for by its own buttons."""
    topic, _ = await thread(client, monkeypatch)
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Reports"})).json()["id"]
    await client.put(f"/projects/{project}", json={"deliverable": {"shape": "report"}})

    def offered(objective: str) -> int:
        with state.session_factory() as session, session.begin():
            offer = RoomMessage(
                topic_id=UUID(topic),
                author_kind="office",
                author_name="Office",
                body="Start this?",
                actions_json=[{"kind": "start_work", "objective": objective}],
            )
            session.add(offer)
            session.flush()
            return int(offer.id)

    def workflow_of(objective: str) -> str:
        with state.session_factory() as session:
            from app.db.models import Task

            run = session.scalars(
                select(Run)
                .join(Task, Task.id == Run.root_task_id)
                .where(Task.objective == objective)
            ).one()
            return run.workflow_name

    first = offered("Compare three CRMs")
    assert (
        await client.post(f"/topics/{topic}/messages/{first}/actions/0", json={})
    ).status_code == 201
    assert workflow_of("Compare three CRMs") == "lead"

    refused = await client.put("/settings", json={"answering": "team"})
    assert refused.status_code == 422, "there is no such setting any more"
    second = offered("Compare three ERPs")
    assert (
        await client.post(f"/topics/{topic}/messages/{second}/actions/0", json={})
    ).status_code == 201
    assert workflow_of("Compare three ERPs") == "lead"
