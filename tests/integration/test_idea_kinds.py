"""The idea wall in three kinds: Friction, Curiosity, Opportunity."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import update

from app import widgets
from app.db.models import Idea


async def keep(client: Any, text: str, kind: str | None = None) -> dict[str, Any]:  # noqa: F811
    body: dict[str, Any] = {"text": text}
    if kind is not None:
        body["kind"] = kind
    kept = await client.post("/ideas", json=body)
    assert kept.status_code == 201, kept.text
    return dict(kept.json())


async def test_an_idea_is_kept_with_its_kind(client: httpx.AsyncClient) -> None:  # noqa: F811
    friction = await keep(client, "Copying invoices by hand again", "friction")
    plain = await keep(client, "Something to think about")
    assert friction["kind"] == "friction"
    assert plain["kind"] is None, "no kind is unsorted"
    refused = await client.post("/ideas", json={"text": "x", "kind": "annoyance"})
    assert refused.status_code == 422


async def test_the_board_lists_open_ideas_by_kind(client: httpx.AsyncClient) -> None:  # noqa: F811
    await keep(client, "Expense reports take an hour", "friction")
    await keep(client, "Local-first sync engines", "curiosity")
    await keep(client, "Clinics still fax referrals", "opportunity")
    await keep(client, "An old thought")
    board = (await client.get("/ideas")).json()
    kinds = board["kinds"]
    assert [i["text"] for i in kinds["friction"]] == ["Expense reports take an hour"]
    assert [i["text"] for i in kinds["curiosity"]] == ["Local-first sync engines"]
    assert [i["text"] for i in kinds["opportunity"]] == ["Clinics still fax referrals"]
    assert [i["text"] for i in kinds["unsorted"]] == ["An old thought"]
    assert board["kind_counts"] == {"friction": 1, "curiosity": 1, "opportunity": 1, "unsorted": 1}
    assert [i["text"] for i in board["new"]][-1] == "Expense reports take an hour", (
        "the By day lists are unchanged"
    )


async def test_todays_three_counts_what_was_kept_today(client: httpx.AsyncClient) -> None:  # noqa: F811
    state = client.app_state  # type: ignore[attr-defined]
    old = await keep(client, "Yesterday's friction", "friction")
    with state.session_factory() as session, session.begin():
        session.execute(
            update(Idea)
            .where(Idea.id == UUID(old["id"]))
            .values(created_at=datetime.now(UTC) - timedelta(days=2))
        )
    assert (await client.get("/ideas")).json()["today_three"] == {
        "friction": 0,
        "curiosity": 0,
        "opportunity": 0,
    }
    done = await keep(client, "Vector databases in Postgres", "curiosity")
    await client.post(f"/ideas/{done['id']}/finish", json={"status": "done"})
    await keep(client, "Another curious thing", "curiosity")
    await keep(client, "Nobody books dog groomers online", "opportunity")
    three = (await client.get("/ideas")).json()["today_three"]
    assert three == {"friction": 0, "curiosity": 2, "opportunity": 1}, "done ones still count"


async def test_a_kind_is_changed_or_cleared_with_one_tap(client: httpx.AsyncClient) -> None:  # noqa: F811
    idea = await keep(client, "Unsorted at first")
    tagged = await client.put(f"/ideas/{idea['id']}/kind", json={"kind": "opportunity"})
    assert tagged.status_code == 200 and tagged.json()["kind"] == "opportunity"
    cleared = await client.put(f"/ideas/{idea['id']}/kind", json={"kind": None})
    assert cleared.json()["kind"] is None
    wrong = await client.put(f"/ideas/{idea['id']}/kind", json={"kind": "other"})
    assert wrong.status_code == 422
    await client.post(f"/ideas/{idea['id']}/finish", json={"status": "dropped"})
    closed = await client.put(f"/ideas/{idea['id']}/kind", json={"kind": "friction"})
    assert closed.status_code == 409 and closed.json()["detail"] == "already_done"


async def test_agents_read_the_kind_of_a_planned_idea(client: httpx.AsyncClient) -> None:  # noqa: F811
    state = client.app_state  # type: ignore[attr-defined]
    idea = await keep(client, "Clinics still fax referrals", "opportunity")
    today = (await client.get("/ideas")).json()["today_is"]
    await client.put(f"/ideas/{idea['id']}/plan", json={"day": today})
    with state.session_factory() as session:
        told = "\n".join(widgets.agent_context(session, datetime.now(UTC), "task"))
    assert "Opportunity: Clinics still fax referrals" in told


async def test_slack_words_keep_an_idea_with_its_kind() -> None:
    from app.channels.inbound import idea_said

    assert idea_said("friction: approvals by email") == ("approvals by email", "friction")
    assert idea_said("Curiosity – edge inference chips") == ("edge inference chips", "curiosity")
    assert idea_said("opportunity: dentists hate scheduling") == (
        "dentists hate scheduling",
        "opportunity",
    )
    assert idea_said("摩擦：每天手动对账") == ("每天手动对账", "friction")
    assert idea_said("好奇：本地优先的同步引擎") == ("本地优先的同步引擎", "curiosity")
    assert idea_said("机会：诊所还在用传真") == ("诊所还在用传真", "opportunity")
    assert idea_said("idea: plain thought") == ("plain thought", None)
    assert idea_said("💡 a bulb") == ("a bulb", None)
    assert idea_said("what is the weather") is None
