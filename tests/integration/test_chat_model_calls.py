"""Model calls behind discussion replies and Slack, kept as a task's are."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, func, select, update

from app.db.models import ModelCall, RoomMessage
from app.gateway.client import Completion, Usage
from app.model_calls import SLACK_KEPT_FOR, ModelCalls, request_of
from tests.integration.api_support import _room_with_team

pytestmark = pytest.mark.integration


def said(text: str) -> Completion:
    return Completion(model_alias="m", model="m", content=text, usage=Usage(), latency_seconds=0.1)


async def replied(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, int]:
    """A discussion where an agent answered the owner; its id and the reply's."""
    await _room_with_team(client, monkeypatch)
    started = await client.post("/topics", json={"text": "Is it raining in Lyon?"})
    topic_id = started.json()["topic"]["id"]
    feed = (await client.get(f"/topics/{topic_id}/feed")).json()
    reply = next(
        i for i in feed["items"] if i.get("kind") == "message" and i["author"]["kind"] == "agent"
    )
    return topic_id, int(str(reply["id"]).removeprefix("m:"))


async def test_a_discussion_reply_keeps_the_calls_behind_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic_id, reply = await replied(client, monkeypatch)

    listed = await client.get(f"/topics/{topic_id}/messages/{reply}/model-calls")

    assert listed.status_code == 200, listed.text
    calls = listed.json()
    assert calls and all(c["origin"] == "room" and c["run_id"] is None for c in calls)
    whole = (await client.get(f"/model-calls/{calls[0]['id']}")).json()
    assert "Is it raining in Lyon?" in str(whole["messages"])
    assert whole["response"]["content"]


async def test_a_discussions_calls_go_with_its_messages(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic_id, _ = await replied(client, monkeypatch)
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        session.execute(delete(RoomMessage).where(RoomMessage.topic_id == UUID(topic_id)))
    with factory() as session:
        left = session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.topic_id == UUID(topic_id))
        )
    assert left == 0


async def test_a_reply_that_is_not_an_agents_has_no_calls(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic_id, _ = await replied(client, monkeypatch)
    feed = (await client.get(f"/topics/{topic_id}/feed")).json()
    owner = next(
        i for i in feed["items"] if i.get("kind") == "message" and i["author"]["kind"] == "owner"
    )
    asked = await client.get(
        f"/topics/{topic_id}/messages/{str(owner['id']).removeprefix('m:')}/model-calls"
    )
    assert (asked.status_code, asked.json()) == (200, [])
    assert (await client.get(f"/topics/{topic_id}/messages/999999/model-calls")).status_code == 404


async def test_slack_calls_are_kept_for_a_while_then_let_go(client: Any) -> None:
    factory = client.app_state.session_factory
    calls = ModelCalls(factory)
    shaped, bodies = request_of("chief", [{"role": "user", "content": "Hi"}], None, None, None)
    old = calls.keep(None, "chief", shaped, bodies, said("Hello."), "ok", 0.2, chat_ref="slack:a")
    with factory() as session, session.begin():
        session.execute(
            update(ModelCall)
            .where(ModelCall.id == old)
            .values(created_at=datetime.now(UTC) - SLACK_KEPT_FOR - timedelta(hours=1))
        )

    new = calls.keep(None, "chief", shaped, bodies, said("Hi."), "ok", 0.2, chat_ref="slack:b")

    with factory() as session:
        kept = set(session.scalars(select(ModelCall.id).where(ModelCall.origin == "slack")))
    assert kept == {new}
