"""A thread's own settings from its composer: model, access, goal, plan first."""

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.gateway.client import Completion, Usage
from tests.integration.api_support import _connection, new_agent


class Recorded:
    """Stands in for the reply model: who answered, on what, and what it read."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def __call__(self, agent: dict[str, Any], messages: list[dict], **options: Any) -> Any:
        prompt = "\n".join(str(m["content"]) for m in messages)
        self.calls.append((agent.get("connection_id"), prompt))
        return Completion(
            model_alias="fake",
            model="fake",
            content="Here is my answer.",
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            latency_seconds=0.1,
        )


async def thread(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, Recorded]:
    recorded = Recorded()
    monkeypatch.setattr("app.room_replies.RoomReplies.complete", recorded)
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    await new_agent(client, name="Cato", role="chief")
    started = await client.post("/topics", json={"text": "Pricing for the new site"})
    return str(started.json()["topic"]["id"]), recorded


async def test_a_thread_starts_with_the_office_defaults(client: httpx.AsyncClient) -> None:  # noqa: F811
    topic = (await client.post("/topics", json={"text": "Defaults"})).json()["topic"]["id"]
    view = (await client.get(f"/topics/{topic}/settings")).json()
    assert view == {
        "model": None,
        "access": "read",
        "goal": None,
        "goal_cap_usd": None,
        "cap_usd": 0.5,
        "work_available": False,
        "default_model": None,
    }


async def test_settings_are_saved_and_refused_with_a_reason(client: httpx.AsyncClient) -> None:  # noqa: F811
    topic = (await client.post("/topics", json={"text": "Settings"})).json()["topic"]["id"]
    model = _connection(client, "Flash")
    saved = await client.put(
        f"/topics/{topic}/settings",
        json={"model_connection_id": model, "goal": "Ship pricing", "goal_cap_usd": 2.0},
    )
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert view["model"]["name"] == "Flash" and view["goal"] == "Ship pricing"
    assert view["cap_usd"] == 2.0
    work = await client.put(f"/topics/{topic}/settings", json={"access": "work"})
    assert work.status_code == 409 and work.json()["detail"] == "work_access_off"
    cleared = await client.put(f"/topics/{topic}/settings", json={"model_connection_id": None})
    assert cleared.json()["model"] is None and cleared.json()["goal"] == "Ship pricing"
    too_much = await client.put(f"/topics/{topic}/settings", json={"goal_cap_usd": 500})
    assert too_much.status_code == 422


async def test_an_unchecked_connection_cannot_be_the_threads_model(
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    topic = (await client.post("/topics", json={"text": "Unchecked"})).json()["topic"]["id"]
    created = await client.post(
        "/connections", json={"provider": "groq", "display_name": "Fresh", "model_id": "m"}
    )
    refused = await client.put(
        f"/topics/{topic}/settings", json={"model_connection_id": created.json()["id"]}
    )
    assert refused.status_code == 409 and refused.json()["detail"] == "connection_not_ready"


async def test_replies_use_the_threads_model_and_hear_its_goal(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic, recorded = await thread(client, monkeypatch)
    model = _connection(client, "Flash")
    await client.put(
        f"/topics/{topic}/settings",
        json={"model_connection_id": model, "goal": "Decide the launch price"},
    )
    recorded.calls.clear()
    sent = await client.post(f"/topics/{topic}/messages", json={"text": "@Cato what next?"})
    assert sent.status_code == 201, sent.text
    assert recorded.calls, "the lead replied"
    used, prompt = recorded.calls[-1]
    assert used == model, "the thread's model answered"
    assert "The owner's goal for this discussion: Decide the launch price" in prompt


async def test_a_plan_first_message_asks_for_a_plan(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic, recorded = await thread(client, monkeypatch)
    recorded.calls.clear()
    await client.post(f"/topics/{topic}/messages", json={"text": "@Cato redo the pricing page"})
    assert "asked for a plan first" not in recorded.calls[-1][1]
    recorded.calls.clear()
    sent = await client.post(
        f"/topics/{topic}/messages", json={"text": "@Cato redo the pricing page", "plan": True}
    )
    assert sent.status_code == 201
    assert "asked for a plan first" in recorded.calls[-1][1]
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session:
        from app.db.models import RoomMessage

        asked = session.get(RoomMessage, int(sent.json()["id"].removeprefix("m:")))
        assert asked is not None
        assert {"kind": "plan_first"} in (asked.actions_json or [])


async def test_work_from_a_thread_takes_its_goals_cap(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic, _ = await thread(client, monkeypatch)
    await client.put(f"/topics/{topic}/settings", json={"goal_cap_usd": 3.0})
    state = client.app_state  # type: ignore[attr-defined]
    from uuid import UUID

    from app.db.models import RoomMessage, Run

    with state.session_factory() as session, session.begin():
        offer = RoomMessage(
            topic_id=UUID(topic),
            author_kind="office",
            author_name="Office",
            body="Start this as a piece of work?",
            actions_json=[{"kind": "start_work", "objective": "Draft the pricing page"}],
        )
        session.add(offer)
        session.flush()
        offer_id = offer.id
    pressed = await client.post(f"/topics/{topic}/messages/{offer_id}/actions/0", json={})
    assert pressed.status_code == 201, pressed.text
    with state.session_factory() as session:
        run = session.scalars(select(Run).where(Run.topic_id == UUID(topic))).first()
        assert run is not None
        assert (run.budget_json or {}).get("max_cost_usd") == 3.0


async def test_the_status_line_counts_the_thread(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 12c: turns, replies, the work's steps, tokens and cost."""
    from datetime import UTC, datetime
    from uuid import UUID

    from app.db.models import RoomMessage, Run, UsageEvent

    topic, _ = await thread(client, monkeypatch)
    await client.post(f"/topics/{topic}/messages", json={"text": "@Cato and then?"})
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session:
        replied = session.scalars(
            select(RoomMessage).where(
                RoomMessage.topic_id == UUID(topic), RoomMessage.author_kind == "agent"
            )
        ).all()
    reply_tokens = sum(int(m.tokens or 0) for m in replied)
    project = (await client.post("/projects", json={"name": "Usage"})).json()["id"]
    dispatch = state.repository.create(UUID(project), "Price the plans")
    with state.session_factory() as session, session.begin():
        session.get(Run, dispatch.run_id).topic_id = UUID(topic)  # type: ignore[union-attr]
        for role in ("researcher", "chief", "tools"):
            session.add(
                UsageEvent(
                    run_id=dispatch.run_id,
                    task_id=dispatch.task_id,
                    project_id=dispatch.project_id,
                    role=role,
                    provider="x",
                    model_alias="a",
                    status="ok",
                    prompt_tokens=100,
                    completion_tokens=50,
                    estimated_cost=0.01,
                    attempts=1,
                    latency_seconds=1.0,
                    occurred_at=datetime.now(UTC),
                )
            )
    line = (await client.get(f"/topics/{topic}/usage")).json()
    assert (line["turns"], line["replies"], line["work"]) == (2, len(replied), 1)
    assert line["steps"] == 2, "tool calls are not model steps"
    assert line["tokens"] == reply_tokens + 300
    assert line["cost_usd"] >= 0.02
    assert set(line["context"]) == {"used", "window", "percent"}


async def test_a_thread_downloads_as_a_zip(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import zipfile

    topic, _ = await thread(client, monkeypatch)
    await client.put(f"/topics/{topic}/settings", json={"goal": "Decide the launch price"})
    got = await client.get(f"/topics/{topic}/export.zip")
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/zip"
    assert 'filename="pricing-for-the-new-site.zip"' in got.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(got.content))
    assert sorted(archive.namelist()) == ["attachments.txt", "thread.md", "work.md"]
    thread_md = archive.read("thread.md").decode()
    assert "Decide the launch price" in thread_md and "Pricing for the new site" in thread_md
    assert "Here is my answer." in thread_md
    assert "Nothing was started" in archive.read("work.md").decode()
