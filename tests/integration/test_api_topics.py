"""Discussions: topics, rooms, replies, decisions and drafts."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text, update

from app.db.models import Run as RunRow
from tests.integration.api_support import (
    ANSWER,
    BRIEF,
    _agent_id,
    _agent_says,
    _asked_last,
    _delivered_in,
    _hand_off,
    _line_with_project,
    _offers,
    _room_with_team,
    _running_in_a_thread,
    _said_in,
    _topic_with,
    _work_in,
    attached_document,
    calendar_link_server,
    calendar_soon,
    mac_with_calendars,
)

pytestmark = pytest.mark.integration


async def test_an_idea_belongs_to_a_project_and_starts_there(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ideas: a project for an idea; Start opens its discussion in that project."""
    await _topic_with(client, monkeypatch, "talk")
    project = (await client.post("/projects", json={"name": "Colouring site"})).json()
    idea = (await client.post("/ideas", json={"text": "A print button"})).json()
    placed = await client.put(f"/ideas/{idea['id']}/project", json={"project_id": project["id"]})
    assert placed.json()["project"] == {"id": project["id"], "name": "Colouring site"}
    missing = await client.put(f"/ideas/{idea['id']}/project", json={"project_id": str(uuid4())})
    assert missing.status_code == 404
    started = (await client.post(f"/ideas/{idea['id']}/start")).json()
    topic = (await client.get(f"/topics/{started['topic_id']}")).json()
    assert topic["project"]["id"] == project["id"]
    other = (await client.post("/ideas", json={"text": "Loose thought"})).json()
    freed = await client.put(f"/ideas/{other['id']}/project", json={"project_id": None})
    assert freed.json()["project"] is None


async def test_the_office_answers_about_the_task_on_screen(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser says which task is open; the office answers about that one."""
    from app.office_chat import OfficeChat

    asked: dict[str, Any] = {}

    async def converse(
        self: Any, history: list[dict[str, str]], text: str, agent: Any = None, **kept: Any
    ):
        asked["history"] = history
        asked["text"] = text
        return "It stopped at the critic."

    monkeypatch.setattr("app.channels.inbound_office.SlackOffice.converse", converse)
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Chat {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Compare KYC"})
    ).json()

    response = await client.post(
        "/chat", json={"text": "Why did this stop?", "view": "task", "run_id": run["id"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "It stopped at the critic."
    # The context is read from the office's own records, not taken on trust.
    assert run["id"] in body["context"]
    assert "Compare KYC" in body["context"]
    assert any(run["id"] in turn["content"] for turn in asked["history"])
    assert OfficeChat is not None


# Topics. Talk starts from a sentence; a project's room is its first topic.


async def test_a_topic_starts_from_a_sentence_with_no_project(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = await _room_with_team(client, monkeypatch, {"Cato": "Narrow it to one question."})
    started = await client.post(
        "/topics", json={"text": "I think our onboarding is too complicated."}
    )
    assert started.status_code == 201, started.text
    topic = started.json()["topic"]
    assert topic["project"] is None and topic["title"]
    assert [who["name"] for who in started.json()["replying"]] == ["Cato"], "the lead answers"
    feed = (await client.get(f"/topics/{topic['id']}/feed")).json()
    said = [(i["author"]["kind"], i["text"]) for i in feed["items"] if i["kind"] == "message"]
    assert said == [
        ("owner", "I think our onboarding is too complicated."),
        ("agent", "Narrow it to one question."),
    ]
    assert "belongs to no project yet" in fake.asked[0][1]
    listed = (await client.get("/topics")).json()
    assert listed[0]["id"] == topic["id"] and listed[0]["last"]["kind"] == "agent"


async def test_a_topic_can_be_placed_in_a_project_and_freed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, _ = await _room_with_team(client, monkeypatch)
    topic = (await client.post("/topics", json={"text": "Pricing ideas"})).json()["topic"]
    placed = (await client.put(f"/topics/{topic['id']}", json={"project_id": project_id})).json()
    assert placed["project"]["id"] == project_id and placed["scope_set_by"] == "owner"
    freed = (await client.put(f"/topics/{topic['id']}", json={"project_id": None})).json()
    assert freed["project"] is None and freed["scope_set_by"] is None
    renamed = (await client.put(f"/topics/{topic['id']}", json={"title": "Pricing"})).json()
    assert renamed["title"] == "Pricing"
    home = (await client.get(f"/projects/{project_id}/topic")).json()
    moved = (await client.put(f"/topics/{home['id']}", json={"project_id": None})).json()
    assert moved["project"]["id"] == project_id, "a project's first topic stays with it"


async def test_a_reply_or_thread_must_be_in_the_same_topic(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _room_with_team(client, monkeypatch)
    first = (await client.post("/topics", json={"text": "One"})).json()
    second = (await client.post("/topics", json={"text": "Two"})).json()
    theirs = first["message"]["id"]
    for field in ("reply_to", "thread_id"):
        refused = await client.post(
            f"/topics/{second['topic']['id']}/messages", json={"text": "hi", field: theirs}
        )
        assert refused.status_code == 404, field


async def test_note_this_files_an_idea_and_starts_nothing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, _ = await _topic_with(client, monkeypatch, "note")
    await client.post(
        f"/topics/{topic['id']}/messages", json={"text": "Note this: sell through agencies."}
    )
    said = await _said_in(client, topic["id"])
    assert said[-1]["author"]["kind"] == "office"
    assert said[-1]["text"].startswith("Noted as an idea")
    assert fake.asked == [], "no agent is paid to answer a note"
    seen = (await client.get(f"/topics/{topic['id']}")).json()
    assert [i["text"] for i in seen["ideas"]] == ["Note this: sell through agencies."]
    runs = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"]
    assert runs == []


async def test_a_clear_request_is_answered_and_offered_never_started(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner, 4 Oct: "a task was started immediately without consent". Asked
    for work, the agents answer what they can and the office offers it; it
    starts only when the owner presses Start."""
    topic, fake, _ = await _topic_with(client, monkeypatch, "do", confidence=1.0)
    await client.post(
        f"/topics/{topic['id']}/messages", json={"text": "Compare five services on price."}
    )
    assert (await client.get(f"/topics/{topic['id']}/feed")).json()["work"] == []
    assert fake.asked, "the agents answer first"
    offers = await _offers(client, topic["id"])
    assert len(offers) == 1 and "Nothing starts until you press Start" in offers[0]["text"]


async def test_pressing_start_starts_work_tied_to_the_topic(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, fake, _ = await _topic_with(client, monkeypatch, "do")
    sent = await _hand_off(
        client, topic["id"], "Compare five services like this: scope, price, source."
    )
    feed = (await client.get(f"/topics/{topic['id']}/feed")).json()
    assert len(feed["work"]) == 1
    work = feed["work"][0]
    with state.session_factory() as session:
        run = session.get(RunRow, UUID(work["run_id"]))
        assert run is not None
        assert str(run.topic_id) == topic["id"]
        assert f"m:{run.origin_message_id}" == sent["id"]
        assert run.budget_json["max_cost_usd"] == 0.5
        from app.db.models import Project

        project = session.get(Project, run.project_id)
        assert project is not None and project.name == "Free exploration"
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Started") and "$0.50" in said[-1]["text"]


async def test_an_unsure_reading_is_only_answered(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Start button on every other message was noise: only a clear
    request for work is offered."""
    topic, fake, _ = await _topic_with(client, monkeypatch, "do", confidence=0.6)
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "Maybe check the prices?"})
    assert fake.asked
    assert await _offers(client, topic["id"]) == []


async def test_an_offer_starts_once(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, _ = await _topic_with(client, monkeypatch, "do")
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "Check the prices."})
    offer = (await _offers(client, topic["id"]))[-1]
    message_id = offer["id"].removeprefix("m:")
    pressed = await client.post(f"/topics/{topic['id']}/messages/{message_id}/actions/0", json={})
    assert pressed.status_code == 201, pressed.text
    assert len((await client.get(f"/topics/{topic['id']}/feed")).json()["work"]) == 1
    again = await client.post(f"/topics/{topic['id']}/messages/{message_id}/actions/0", json={})
    assert again.status_code == 409


async def test_stop_pauses_only_this_topics_work(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, _, judge = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Compare the prices.")
    elsewhere = state.repository.create(
        UUID((await client.post("/projects", json={"name": f"P {uuid4().hex[:6]}"})).json()["id"]),
        "Unrelated",
    )
    judge.kind = "stop"
    await client.post(
        f"/topics/{topic['id']}/messages", json={"text": "Hold off, wrong direction."}
    )
    work = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"]
    assert [w["status"] for w in work] == ["PAUSED"]
    with state.session_factory() as session:
        other = session.get(RunRow, elsewhere.run_id)
        assert other is not None and other.status == "PENDING"
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Paused")


async def test_later_sets_when_the_topic_comes_back(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, _ = await _topic_with(client, monkeypatch, "later")
    await client.post(
        f"/topics/{topic['id']}/messages", json={"text": "Let's look again next week."}
    )
    seen = (await client.get(f"/topics/{topic['id']}")).json()
    assert seen["revisit_at"] is not None
    returns = datetime.fromisoformat(seen["revisit_at"])
    assert 5 <= (returns - datetime.now(UTC)).days <= 8
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("I'll bring this back on")
    assert fake.asked == []


async def test_the_delivery_comes_back_into_the_topic(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Compare the prices.")
    work = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"][0]
    run_id = UUID(work["run_id"])
    described = state.repository.describe([run_id])[0][0]
    state.repository.record_stage(
        described, "synthesis", {"recommendation": "Three of five publish prices; two do not."}
    )
    with state.session_factory() as session, session.begin():
        session.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status="COMPLETED", completed_at=datetime.now(UTC))
        )
    state.repository.add_event(run_id, "run_finished", {"status": "COMPLETED"})
    items = (await client.get(f"/topics/{topic['id']}/feed")).json()["items"]
    delivered = [i for i in items if i["kind"] == "delivery"]
    assert len(delivered) == 1 and delivered[0]["run_id"] == str(run_id)
    assert "Three of five publish prices" in delivered[0]["text"]


async def test_without_a_judgment_talk_is_answered_as_before(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = await _room_with_team(client, monkeypatch)
    monkeypatch.setattr("app.handoff.judge_for", lambda settings: None)
    topic = (await client.post("/topics", json={"text": "Look into DNB's fees"})).json()["topic"]
    assert fake.asked, "the lead answers"
    assert (await client.get(f"/topics/{topic['id']}/feed")).json()["work"] == []


async def test_a_quiet_discussion_gets_a_brief_whose_cost_counts(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from sqlalchemy import update as sql_update

    from app import topic_briefs
    from app.db.models import Topic
    from app.office_settings import spent_this_month

    state = client.app_state  # type: ignore[attr-defined]
    _, fake = await _room_with_team(client, monkeypatch, {"Cato": "We could."})
    topic = (await client.post("/topics", json={"text": "A first paid service?"})).json()["topic"]
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "Diagnosis first?"})
    with state.session_factory() as session:
        assert topic_briefs.quiet(session, datetime.now(UTC)) == [], "not quiet yet"
        before = spent_this_month(session)
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(Topic)
            .where(Topic.id == UUID(topic["id"]))
            .values(last_activity_at=datetime.now(UTC) - timedelta(minutes=30))
        )
    with state.session_factory() as session:
        assert topic_briefs.quiet(session, datetime.now(UTC)) == [UUID(topic["id"])]
    fake.answers["Cato"] = BRIEF
    brief = await topic_briefs.refresh(state.session_factory, UUID(topic["id"]), fake)
    assert brief is not None and brief["decided"] == "Start with a human-delivered diagnosis."
    seen = (await client.get(f"/topics/{topic['id']}")).json()
    assert seen["brief"]["open"] == "Include a follow-up call?"
    with state.session_factory() as session:
        assert spent_this_month(session) == pytest.approx(before + 0.01)
        assert topic_briefs.quiet(session, datetime.now(UTC)) == [], "nothing new since"


async def test_coming_back_after_a_while_shows_the_brief_once(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from sqlalchemy import update as sql_update

    from app.db.models import Topic

    state = client.app_state  # type: ignore[attr-defined]
    await _room_with_team(client, monkeypatch)
    topic = (await client.post("/topics", json={"text": "Pricing"})).json()["topic"]
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(Topic)
            .where(Topic.id == UUID(topic["id"]))
            .values(
                brief_json={"where": "x", "decided": "y", "open": "z", "through_message_id": 1},
                owner_seen_at=datetime.now(UTC) - timedelta(days=3),
            )
        )
    assert (await client.get(f"/topics/{topic['id']}")).json()["welcome_back"] is True
    await client.post(f"/topics/{topic['id']}/seen")
    assert (await client.get(f"/topics/{topic['id']}")).json()["welcome_back"] is False


async def test_bringing_a_colleague_in_gives_them_the_brief(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update as sql_update

    from app.db.models import Topic

    state = client.app_state  # type: ignore[attr-defined]
    _, fake = await _room_with_team(client, monkeypatch)
    topic = (await client.post("/topics", json={"text": "Pricing"})).json()["topic"]
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(Topic)
            .where(Topic.id == UUID(topic["id"]))
            .values(brief_json={"where": "Five services compared.", "decided": "", "open": ""})
        )
    vera = await _agent_id(client, "Vera")
    joined = await client.post(f"/topics/{topic['id']}/participants", json={"profile_id": vera})
    assert joined.status_code == 201, joined.text
    assert [p["name"] for p in joined.json()["participants"]] == ["Vera"]
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Vera joined")
    fake.asked.clear()
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "@Vera is that enough?"})
    assert "Five services compared." in fake.asked[0][1], "the brief, not just the history"
    # Naming someone brings them in too.
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "@Iris and you?"})
    names = [p["name"] for p in (await client.get(f"/topics/{topic['id']}")).json()["participants"]]
    assert names == ["Vera", "Iris"]
    gone = await client.delete(f"/topics/{topic['id']}/participants/{vera}")
    assert gone.status_code == 200
    assert [p["name"] for p in gone.json()["participants"]] == ["Iris"]


async def test_new_work_does_not_ask_every_colleague_for_a_word(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each colleague used to add "one thing to take into account" to new work:
    paid, generic, and a question the owner then answered as if it were new
    work. New work now starts quietly; colleagues speak when spoken to."""
    topic, fake, judge = await _topic_with(client, monkeypatch, "talk")
    for name in ("Vera", "Wren"):
        await client.post(
            f"/topics/{topic['id']}/participants",
            json={"profile_id": await _agent_id(client, name)},
        )
    judge.kind = "do"
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "Compare the prices."})
    fake.asked.clear()
    before = len(await _said_in(client, topic["id"]))
    offer = (await _offers(client, topic["id"]))[-1]
    await client.post(
        f"/topics/{topic['id']}/messages/{offer['id'].removeprefix('m:')}/actions/0", json={}
    )
    assert fake.asked == [], "starting the work asks no colleague for a word"
    said = (await _said_in(client, topic["id"]))[before:]
    assert [m for m in said if m["author"]["kind"] == "agent"] == []


async def test_answering_an_agents_question_goes_to_its_work_not_new_work(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, judge = await _topic_with(client, monkeypatch, "do", confidence=1.0)
    asked = await _hand_off(client, topic["id"], "Plan a hike in the Pyrenes for 15-20 October.")
    question = _agent_says(client, topic["id"], asked["id"], "Do you mean the Pyrenees?")
    judge.read.clear()
    await client.post(
        f"/topics/{topic['id']}/messages", json={"text": "是的", "reply_to": question}
    )
    work = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"]
    assert len(work) == 1, "an answer is never new work"
    assert judge.read == [], "an answer to work in progress needs no judging"
    controls = (await client.get(f"/runs/{work[0]['run_id']}/controls")).json()
    told = [c["text"] for c in controls if c["kind"] == "instruction"]
    assert len(told) == 1 and "Do you mean the Pyrenees?" in told[0] and "是的" in told[0]
    said = await _said_in(client, topic["id"])
    assert said[-1]["author"]["kind"] == "office" and "Passed to" in said[-1]["text"]


async def test_a_reply_never_starts_work_by_itself(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, judge = await _topic_with(client, monkeypatch, "talk")
    first = (
        await client.post(f"/topics/{topic['id']}/messages", json={"text": "How is it going?"})
    ).json()
    remark = _agent_says(client, topic["id"], first["id"], "Prices vary a lot by city.")
    judge.kind, judge.confidence = "look_into", 1.0
    fake.asked.clear()
    await client.post(
        f"/topics/{topic['id']}/messages",
        json={"text": "Then look into the prices in Utrecht.", "reply_to": remark},
    )
    feed = (await client.get(f"/topics/{topic['id']}/feed")).json()
    assert feed["work"] == []
    assert await _offers(client, topic["id"]) == []
    assert fake.asked, "the conversation carries on"


async def test_a_short_answer_never_starts_work(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, judge = await _topic_with(client, monkeypatch, "do", confidence=1.0)
    for answer in ("是的", "ok", "Yes please", "thanks!"):
        await client.post(f"/topics/{topic['id']}/messages", json={"text": answer})
    assert (await client.get(f"/topics/{topic['id']}/feed")).json()["work"] == []


async def test_a_greeting_read_as_a_maybe_is_only_answered(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A greeting with a question once read look_into 0.76 and started paid work."""
    topic, fake, _ = await _topic_with(client, monkeypatch, "look_into", confidence=0.76)
    await client.post(
        f"/topics/{topic['id']}/messages",
        json={"text": "Good morning, any news from yesterday worth knowing?"},
    )
    assert (await client.get(f"/topics/{topic['id']}/feed")).json()["work"] == []
    assert await _offers(client, topic["id"]) == []
    assert fake.asked, "the agents answer the question"


async def test_everyone_named_does_not_hand_the_work_to_one(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "look_into", confidence=1.0)
    await _hand_off(client, topic["id"], "@everyone compare five services.")
    work = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"][0]
    assert work["handed_to"] is None
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Started:")


async def test_asking_a_colleague_hands_the_work_to_them(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, judge = await _topic_with(client, monkeypatch, "look_into")
    await _hand_off(client, topic["id"], "@Iris compare five services.")
    work = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"][0]
    assert work["handed_to"] == "Iris"
    said = await _said_in(client, topic["id"])
    assert "Iris" in said[-1]["text"]


async def test_a_message_branches_into_a_topic_of_its_own(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _room_with_team(client, monkeypatch)
    started = (await client.post("/topics", json={"text": "Pricing a first service"})).json()
    topic = started["topic"]
    aside = (
        await client.post(
            f"/topics/{topic['id']}/messages",
            json={"text": "Actually, we could also sell this through agencies."},
        )
    ).json()
    branched = await client.post(
        f"/topics/{topic['id']}/branch",
        json={
            "message_id": aside["id"],
            "title": "Sell through agencies",
            "brief": "Idea from Pricing a first service: sell a human-delivered diagnosis first.",
        },
    )
    assert branched.status_code == 201, branched.text
    child = branched.json()
    assert child["title"] == "Sell through agencies"
    assert child["parent"]["id"] == topic["id"]
    carried = await _said_in(client, child["id"])
    assert "through agencies" in carried[0]["text"].lower()
    assert "sell a human-delivered diagnosis first" in carried[0]["text"]
    parent = (await client.get(f"/topics/{topic['id']}")).json()
    assert [b["title"] for b in parent["branches"]] == ["Sell through agencies"]
    link = (await _said_in(client, topic["id"]))[-1]
    assert link["actions"][0] == {"kind": "open_topic", "topic_id": child["id"]}
    other = (await client.post("/topics", json={"text": "Elsewhere"})).json()["topic"]
    refused = await client.post(f"/topics/{other['id']}/branch", json={"message_id": aside["id"]})
    assert refused.status_code == 404


async def test_an_idea_on_the_desk_can_be_started_or_dropped(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, judge = await _topic_with(client, monkeypatch, "note")
    for words in ("Note: sell through agencies.", "Note: a referral scheme."):
        await client.post(f"/topics/{topic['id']}/messages", json={"text": words})
    ideas = (await client.get("/office/desk")).json()["ideas"]["new"]
    assert [i["text"] for i in ideas] == [
        "Note: a referral scheme.",
        "Note: sell through agencies.",
    ]
    assert ideas[0]["topic"]["id"] == topic["id"] and ideas[0]["source"] == "discussion"
    panel = (await client.get(f"/topics/{topic['id']}")).json()["ideas"]
    assert len(panel) == 2, "the discussion still lists the ideas noted in it"
    started = await client.post(f"/ideas/{ideas[1]['id']}/start")
    assert started.status_code == 201, started.text
    work = (await client.get(f"/topics/{topic['id']}/feed")).json()["work"]
    assert len(work) == 1
    dropped = await client.post(f"/ideas/{ideas[0]['id']}/drop")
    assert dropped.status_code == 200
    assert (await client.get("/office/desk")).json()["ideas"]["counts"]["new"] == 0


async def test_starting_a_floor_idea_gives_it_a_discussion_of_its_own(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ID2: Start, on the owner's press, opens a discussion with the idea's
    words and starts the work there; the idea leaves the board, taken."""
    await _topic_with(client, monkeypatch, "talk")
    idea = (await client.post("/ideas", json={"text": "Compare three CRMs"})).json()
    started = await client.post(f"/ideas/{idea['id']}/start")
    assert started.status_code == 201, started.text
    topic_id = started.json()["topic_id"]
    said = await _said_in(client, topic_id)
    assert said[0]["author"]["kind"] == "owner" and said[0]["text"] == "Compare three CRMs"
    work = (await client.get(f"/topics/{topic_id}/feed")).json()["work"]
    assert len(work) == 1 and work[0]["run_id"] == started.json()["run_id"]
    assert (await client.get("/ideas")).json()["counts"]["new"] == 0
    twice = await client.post(f"/ideas/{idea['id']}/start")
    assert twice.status_code == 409, "taken once"


async def test_a_discussion_comes_back_on_its_date(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from sqlalchemy import update as sql_update

    from app import desk as office_desk
    from app.db.models import Topic

    state = client.app_state  # type: ignore[attr-defined]
    topic, _, _ = await _topic_with(client, monkeypatch, "later")
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "Next week."})
    parked_desk = (await client.get("/office/desk")).json()
    assert [r["id"] for r in parked_desk["parked"]["returning"]] == [topic["id"]]
    assert topic["id"] not in [t["id"] for t in parked_desk["continue"]], "parked until then"
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(Topic)
            .where(Topic.id == UUID(topic["id"]))
            .values(
                revisit_at=datetime.now(UTC) - timedelta(minutes=1),
                brief_json={"where": "Five compared.", "decided": "", "open": "A call?"},
            )
        )
    assert office_desk.returns_due(state.session_factory, datetime.now(UTC)) == 1
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Back as you asked")
    assert "A call?" in said[-1]["text"]
    desk = (await client.get("/office/desk")).json()
    assert desk["parked"]["returning"] == []
    assert desk["continue"][0]["id"] == topic["id"] and desk["continue"][0]["back"] is True
    assert office_desk.returns_due(state.session_factory, datetime.now(UTC)) == 0


async def test_a_delivery_says_whether_anything_is_needed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Compare the prices.")
    run_id = await _work_in(client, topic["id"], "COMPLETED")
    state.repository.add_event(run_id, "run_finished", {"status": "COMPLETED"})
    items = (await client.get(f"/topics/{topic['id']}/feed")).json()["items"]
    delivery = next(i for i in items if i["kind"] == "delivery")
    assert delivery["text"].endswith("Nothing needed from you now.")


async def test_a_revision_that_needs_new_evidence_offers_a_rework(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as json_module

    topic, fake, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    fake.answers["Cato"] = json_module.dumps(
        {
            "revised": None,
            "note": "That needs the agencies' own price lists.",
            "needs_research": True,
        }
    )
    await client.post(
        f"/topics/{topic['id']}/revise",
        json={"run_id": str(run_id), "quote": "about EUR 5,000", "comment": "Check this."},
    )
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    assert answer["current"]["version"] == 1, "nothing is invented"
    said = await _said_in(client, topic["id"])
    offer = said[-1]
    assert "price lists" in offer["text"]
    assert offer["actions"][0]["kind"] == "rework" and offer["actions"][0]["run_id"] == str(run_id)


async def test_only_this_discussions_work_can_be_revised_here(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    other = (await client.post("/topics", json={"text": "Elsewhere"})).json()["topic"]
    refused = await client.post(
        f"/topics/{other['id']}/revise",
        json={"run_id": str(run_id), "quote": "x", "comment": "y"},
    )
    assert refused.status_code == 404


# A long chat reply, opened in the editor as a document of its own.


async def test_a_chat_reply_opens_as_a_document_and_saves_versions(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import RoomMessage

    state = client.app_state  # type: ignore[attr-defined]
    topic = (await client.post("/topics", json={"text": "Draft privacy terms"})).json()["topic"]
    with state.session_factory() as session, session.begin():
        reply = RoomMessage(
            project_id=None,
            topic_id=UUID(topic["id"]),
            author_kind="agent",
            author_role="chief",
            author_name="Tim",
            body="Privacy Policy\n\nWe respect your privacy.",
        )
        session.add(reply)
        session.flush()
        message_id = reply.id
    opened = await client.post(f"/topics/{topic['id']}/messages/{message_id}/document")
    assert opened.status_code == 201, opened.text
    view = opened.json()
    assert view["current"]["version"] == 1 and view["current"]["by"] == "owner"
    again = (await client.post(f"/topics/{topic['id']}/messages/{message_id}/document")).json()
    assert again["lineage"] == view["lineage"], "opening twice opens the same document"
    shown = (await client.get(f"/artifacts/{view['current']['artifact_id']}/document")).json()
    assert shown["document"]["sections"][0]["heading"] == "Privacy Policy"
    edited = {
        "title": "Privacy terms",
        "sections": [
            {
                "heading": "Privacy Policy",
                "blocks": [{"kind": "paragraph", "text": "We **really** respect it."}],
            }
        ],
    }
    saved = await client.put(
        f"/documents/{view['lineage']}", json={"base_version": 1, "document": edited}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["current"]["version"] == 2
    stale = await client.put(
        f"/documents/{view['lineage']}", json={"base_version": 1, "document": edited}
    )
    assert stale.status_code == 409
    word = await client.get(
        f"/artifacts/{saved.json()['current']['artifact_id']}/download?format=md"
    )
    assert "We **really** respect it." in word.text


async def test_a_decision_is_recorded_from_the_owners_own_words(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import Project
    from app.project_context import build_run_context

    state = client.app_state  # type: ignore[attr-defined]
    await _room_with_team(client, monkeypatch)
    venture_id, project_id = await _line_with_project(client, "Harbor AI", "Pricing")
    home = (await client.get(f"/projects/{project_id}/topic")).json()
    said = (
        await client.post(
            f"/topics/{home['id']}/messages",
            json={"text": "We lead with the self-serve product."},
        )
    ).json()
    first = await client.post(
        f"/topics/{home['id']}/decisions",
        json={"message_id": said["id"], "text": "Lead with the self-serve product."},
    )
    assert first.status_code == 201, first.text
    later = (
        await client.post(
            f"/topics/{home['id']}/messages",
            json={"text": "Actually: start with a human-delivered diagnosis."},
        )
    ).json()
    second = await client.post(
        f"/topics/{home['id']}/decisions",
        json={
            "message_id": later["id"],
            "text": "Start with a human-delivered diagnosis; build the product later.",
            "replaces": first.json()["id"],
        },
    )
    assert second.status_code == 201, second.text
    with state.session_factory() as session:
        project = session.get(Project, UUID(project_id))
        told = build_run_context(session, project, state.worker.settings)["decisions"]
    assert told == ["Start with a human-delivered diagnosis; build the product later."], told
    space = (await client.get(f"/ventures/{venture_id}/space")).json()
    current = [d for d in space["decisions"] if d["status"] == "active"]
    assert [d["text"] for d in current] == [
        "Start with a human-delivered diagnosis; build the product later."
    ]
    assert current[0]["source"]["topic_id"] == home["id"]
    assert [r["text"] for r in current[0]["replaced"]] == ["Lead with the self-serve product."]
    lines = await _said_in(client, home["id"])
    assert lines[-1]["text"].startswith("Recorded as a decision")


async def test_a_decision_needs_a_place_to_hold(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _room_with_team(client, monkeypatch)
    started = (await client.post("/topics", json={"text": "We go agency-first."})).json()
    refused = await client.post(
        f"/topics/{started['topic']['id']}/decisions",
        json={"message_id": started["message"]["id"], "text": "Agency-first."},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "place_first"


async def test_a_message_that_sounds_decided_is_offered_never_recorded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func

    from app.db.models import Decision

    state = client.app_state  # type: ignore[attr-defined]
    topic, fake, _ = await _topic_with(client, monkeypatch, "decide")
    await client.post(f"/topics/{topic['id']}/messages", json={"text": "We go agency-first."})
    said = await _said_in(client, topic["id"])
    assert fake.asked, "the conversation carries on"
    assert said[-1]["actions"][0]["kind"] == "record_decision"
    with state.session_factory() as session:
        assert session.scalar(select(func.count(Decision.id))) == 0, "nothing recorded by itself"


async def test_a_resent_message_is_said_once(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, judge = await _topic_with(client, monkeypatch, "do")
    body = {"text": "Compare the prices.", "client_key": "k-0123456789abcdef"}
    first = (await client.post(f"/topics/{topic['id']}/messages", json=body)).json()
    second = (await client.post(f"/topics/{topic['id']}/messages", json=body)).json()
    assert first["id"] == second["id"]
    assert len(await _offers(client, topic["id"])) == 1, "offered once, not twice"
    start = {"text": "A new thought", "client_key": "k-fedcba9876543210"}
    a = (await client.post("/topics", json=start)).json()
    b = (await client.post("/topics", json=start)).json()
    assert a["topic"]["id"] == b["topic"]["id"]


async def test_an_agent_can_answer_about_work_from_another_discussion(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, fake, judge = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Compare the agencies' onboarding prices.")
    run_id = await _work_in(client, topic["id"], "COMPLETED")
    described = state.repository.describe([run_id])[0][0]
    state.repository.record_stage(
        described, "synthesis", {"recommendation": "Three of five agencies publish prices."}
    )
    judge.kind = "talk"
    other = (await client.post("/topics", json={"text": "Something else entirely"})).json()
    prompt = await _asked_last(
        client, other["topic"]["id"], fake, "What did the agency comparison find?"
    )
    shown = prompt[prompt.find("other recent work") : prompt.find("other recent work") + 600]
    assert "Compare the agencies" in shown, shown
    assert "Three of five agencies publish prices." in shown, shown


async def test_the_ask_chat_and_discussions_see_the_owners_calendar(
    client: httpx.AsyncClient,
) -> None:
    """7 Oct: asked in the Ask chat, the agents could not read the calendar; tasks
    alone were told it. Conversations are now told it too, in the owner's zone."""
    from app.channels.inbound_office import SlackOffice

    state = client.app_state  # type: ignore[attr-defined]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        uuid4(),
    )
    assert "No calendar is connected" in office.facts()["owner_calendar"]
    calendar_link_server(client, calendar_soon("Board review"))
    await client.post(
        "/calendars", json={"kind": "ics", "provider": "google", "url": "https://c.example/a.ics"}
    )
    saved = await client.put("/calendars/preferences", json={"timezone": "Asia/Hong_Kong"})
    assert saved.status_code == 200
    told = office.facts()["owner_calendar"]
    assert "Board review" in told and "Asia/Hong_Kong" in told and "Now it is" in told
    assert "4242" not in told, "titles and times only, as the owner chose"
    from app.room_replies import RoomReplies

    replies = RoomReplies(state.session_factory, state.worker.settings, state.repository.store)
    topic = (await client.post("/topics", json={"text": "What is on my calendar?"})).json()
    with state.session_factory() as session:
        message = session.scalar(
            text("select id from room_messages where topic_id = :t order by id limit 1"),
            {"t": topic["topic"]["id"]},
        )
        prompt = replies._messages(
            session, UUID(topic["topic"]["id"]), int(message), {"name": "Tim", "role": "chief"}
        )
    assert "Board review" in prompt[0]["content"]


async def test_a_proposed_calendar_event_is_added_only_by_the_owners_press(
    client: httpx.AsyncClient,
) -> None:
    """CAL4, 7 Oct: an agent proposes, the owner adds it from the calendar form."""
    from app.db.models import RoomMessage

    await mac_with_calendars(client)
    state = client.app_state  # type: ignore[attr-defined]
    topic = (await client.post("/topics", json={"text": "Block time for the report"})).json()
    topic_id = topic["topic"]["id"]
    start = datetime.now(UTC) + timedelta(days=1)
    proposal = {
        "kind": "add_event",
        "title": "Draft the report",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=2)).isoformat(),
    }
    with state.session_factory() as session, session.begin():
        row = RoomMessage(
            topic_id=UUID(topic_id),
            author_kind="agent",
            author_name="Tim",
            body="Thursday morning is free.",
            actions_json=[proposal],
        )
        session.add(row)
        session.flush()
        message_id = row.id
    path = f"/topics/{topic_id}/messages/{message_id}/actions/0"
    assert (await client.post(path, json={})).json()["detail"] == "change_missing"
    change = (
        await client.post(
            "/calendars/events",
            json={
                "calendar_id": "home",
                "title": "Draft the report",
                "starts_at": proposal["starts_at"],
                "ends_at": proposal["ends_at"],
            },
        )
    ).json()
    pressed = await client.post(path, json={"change_id": change["id"]})
    assert pressed.status_code == 201 and pressed.json()["action"]["change_id"] == change["id"]
    again = await client.post(path, json={"change_id": change["id"]})
    assert again.status_code == 409, "each proposal is added once"


async def test_agents_are_told_how_to_propose_time_only_when_a_calendar_can_take_it(
    client: httpx.AsyncClient,
) -> None:
    from app.room_replies import CALENDAR_RULE, RoomReplies

    state = client.app_state  # type: ignore[attr-defined]
    replies = RoomReplies(state.session_factory, state.worker.settings, state.repository.store)
    topic = (await client.post("/topics", json={"text": "Plan my week"})).json()

    def prompt() -> str:
        with state.session_factory() as session:
            message = session.scalar(
                text("select id from room_messages where topic_id = :t order by id limit 1"),
                {"t": topic["topic"]["id"]},
            )
            return replies._messages(
                session, UUID(topic["topic"]["id"]), int(message), {"name": "Tim", "role": "chief"}
            )[0]["content"]

    assert CALENDAR_RULE not in prompt(), "no calendar connected"
    await mac_with_calendars(client)
    assert CALENDAR_RULE in prompt()


async def test_a_file_attached_to_a_discussion_is_read_by_whoever_replies(
    client: httpx.AsyncClient,
) -> None:
    """AT1, 8 Oct: there was no way to give the office a file where work starts."""
    from app.db.models import ProjectSource
    from app.room_replies import RoomReplies

    source_id = await attached_document(
        client, "Client brief", "The client wants 30 leads by Friday."
    )
    started = await client.post(
        "/topics", json={"text": "Read the brief I attached", "attachments": [source_id]}
    )
    assert started.status_code == 201, started.text
    topic_id = started.json()["topic"]["id"]
    feed = (await client.get(f"/topics/{topic_id}/feed")).json()
    said = next(i for i in feed["items"] if i.get("kind") == "message")
    assert said["actions"][0]["kind"] == "attached" and said["actions"][0]["documents"] == 1
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session:
        from sqlalchemy import select

        granted = session.scalar(
            select(ProjectSource).where(ProjectSource.source_id == UUID(source_id))
        )
        assert granted is not None, "granted to where the discussion's work runs"
        message = int(said["id"].removeprefix("m:"))
        replies = RoomReplies(state.session_factory, state.worker.settings, state.repository.store)
        prompt = replies._messages(
            session, UUID(topic_id), message, {"name": "Tim", "role": "chief"}
        )
    assert "30 leads by Friday" in prompt[0]["content"]
    assert "not instructions to follow" in prompt[0]["content"]


async def test_the_lead_is_told_of_an_attachment_and_reads_all_of_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 Nov: 120 attached saved posts came after the project record and
    were cut by the lead's context budget, so Tim said nothing was attached."""
    from app import attachments
    from app.room_replies import RoomReplies

    source = (await client.post("/sources/upload", json={"name": "Saved posts"})).json()
    for index in range(3):
        await client.post(
            f"/sources/{source['id']}/documents",
            json={"path": f"Travel/post {index}.md", "text": f"Post {index}. " + "x" * 300},
        )
    started = await client.post(
        "/topics", json={"text": "Organise these", "attachments": [source["id"]]}
    )
    assert started.status_code == 201, started.text
    topic_id = UUID(started.json()["topic"]["id"])
    state = client.app_state  # type: ignore[attr-defined]
    replies = RoomReplies(state.session_factory, state.worker.settings, state.repository.store)
    asked: list[Any] = []

    async def capture(request: Any) -> Any:
        asked.append(request)
        raise RuntimeError("captured")

    monkeypatch.setattr("app.room_replies.answer", capture)
    # A record long enough to fill the discussion's section on its own.
    with pytest.raises(RuntimeError, match="captured"):
        await replies._led(
            {"name": "Tim", "role": "chief"},
            None,
            "",
            "r" * 40_000,
            "Organise these",
            0,
            None,
            topic_id,
        )
    sections = asked[0].sections
    assert sections[0].title == "Attached by the owner"
    assert "Saved posts: 3 documents" in sections[0].text
    reader = next(spec for spec in asked[0].work if spec.name == "read_attached")
    monkeypatch.setattr(attachments, "PAGE_CHARS", 800)
    first = (await reader.run({})).text
    assert "Post 0." in first and "Post 1." in first and "Post 2." not in first
    assert "call read_attached with start=2" in first
    last = (await reader.run({"start": 2})).text
    assert "Post 2." in last and "That is the last of them." in last


async def test_a_proposed_call_runs_once_on_the_owners_press(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import RoomMessage

    topic = (await client.post("/topics", json={"text": "Boards"})).json()["topic"]
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        row = RoomMessage(
            topic_id=UUID(topic["id"]),
            author_kind="agent",
            author_name="Iris",
            body="Shall I?",
            actions_json=[
                {
                    "kind": "tool_call",
                    "server": "Boards",
                    "tool": "board_create",
                    "arguments": {"title": "Q4"},
                }
            ],
        )
        session.add(row)
        session.flush()
        message_id = row.id
    pressed_with: list[dict[str, Any]] = []

    async def pressed(
        factory: Any, settings: Any, project: Any, action: dict[str, Any], owner: str
    ) -> str:
        pressed_with.append(action)
        return "Created board Q4"

    monkeypatch.setattr("app.room_tools.pressed", pressed)
    path = f"/topics/{topic['id']}/messages/{message_id}/tool-calls/0"
    first = await client.post(path)
    assert first.status_code == 201, first.text
    assert first.json()["action"]["result_id"]
    assert pressed_with[0]["arguments"] == {"title": "Q4"}
    with factory() as session:
        said = session.get(RoomMessage, first.json()["action"]["result_id"])
        assert said is not None and said.author_kind == "office"
        assert "Created board Q4" in said.body
    assert (await client.post(path)).status_code == 409


async def test_a_refused_press_says_why_and_can_be_tried_again(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import RoomMessage
    from app.mcp.calling import ToolRefused

    topic = (await client.post("/topics", json={"text": "Boards"})).json()["topic"]
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        row = RoomMessage(
            topic_id=UUID(topic["id"]),
            author_kind="agent",
            author_name="Iris",
            body="Shall I?",
            actions_json=[
                {"kind": "tool_call", "server": "Boards", "tool": "board_create", "arguments": {}}
            ],
        )
        session.add(row)
        session.flush()
        message_id = row.id

    async def refused(*args: Any) -> str:
        raise ToolRefused("tool_refused")

    monkeypatch.setattr("app.room_tools.pressed", refused)
    path = f"/topics/{topic['id']}/messages/{message_id}/tool-calls/0"
    first = await client.post(path)
    assert first.status_code == 409 and first.json()["detail"] == "tool_refused"
    with factory() as session:
        row = session.get(RoomMessage, message_id)
        assert row is not None and "running" not in row.actions_json[0]


async def test_queued_messages_go_to_the_thread_when_the_work_ends_not_when_it_pauses(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runtime.events import RunSettled

    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    topic_id, run_id = await _running_in_a_thread(client)
    await client.post(f"/runs/{run_id}/queue", json={"text": "Also add a free tier."})
    events = client.app_state.scope.get("events")  # type: ignore[attr-defined]

    await events.publish(RunSettled(UUID(run_id), "PAUSED"))
    said = [
        i["text"]
        for i in (await client.get(f"/topics/{topic_id}/feed")).json()["items"]
        if i["kind"] == "message"
    ]
    assert "Also add a free tier." not in said

    await events.publish(RunSettled(UUID(run_id), "COMPLETED"))
    items = (await client.get(f"/topics/{topic_id}/feed")).json()["items"]
    mine = [i for i in items if i["kind"] == "message" and i["text"] == "Also add a free tier."]
    assert len(mine) == 1 and mine[0]["author"]["kind"] == "owner"
    controls = (await client.get(f"/runs/{run_id}/controls")).json()
    assert [c["status"] for c in controls if c["kind"] == "queued"] == ["APPLIED"]

    await events.publish(RunSettled(UUID(run_id), "COMPLETED"))
    items = (await client.get(f"/topics/{topic_id}/feed")).json()["items"]
    assert sum(1 for i in items if i.get("text") == "Also add a free tier.") == 1, "delivered once"


async def test_a_reply_keeps_its_steps_and_shows_them_while_it_works(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 31: what the agent is doing shows in the discussion, then stays on the reply."""
    from copy import deepcopy

    from app import room_replies as module
    from app.gateway.client import Completion, ToolCall, Usage
    from app.room_replies import RoomReplies

    state = client.app_state  # type: ignore[attr-defined]
    replies = RoomReplies(state.session_factory, state.worker.settings, state.repository.store)
    topic = (await client.post("/topics", json={"text": "What did I decide?"})).json()
    topic_id = UUID(topic["topic"]["id"])
    seen_while_working: list[Any] = []

    class Stand:
        turns = [
            Completion(
                model_alias="m",
                model="m",
                content="",
                usage=Usage(),
                latency_seconds=0.0,
                tool_calls=[ToolCall(id="1", name="search_memory", arguments={"query": "pricing"})],
            ),
            Completion(
                model_alias="m",
                model="m",
                content="Nothing on that.",
                usage=Usage(),
                latency_seconds=0.0,
            ),
        ]

        async def complete(self, alias: str, messages: list[Any], **options: Any) -> Completion:
            seen_while_working.append(deepcopy(replies.replying.get(topic_id)))
            return self.turns.pop(0)

    with state.session_factory() as session:
        message = int(
            session.scalar(
                text("select id from room_messages where topic_id = :t order by id limit 1"),
                {"t": str(topic_id)},
            )
        )
    agent = {"name": "Tim", "role": "chief"}
    replies._parts = lambda *args: ("stable", "context", "room")  # type: ignore[method-assign]
    monkeypatch.setattr(module, "KeptCompleter", lambda *args, **kwargs: Stand())
    entry: dict[str, Any] = {**module.view(agent), "steps": []}
    replies.replying[topic_id] = [entry]

    assert await replies._reply(topic_id, message, agent, module.StepLog(entry["steps"]))

    assert seen_while_working[1][0]["steps"][0]["tool"] == "search_memory", "seen before the reply"
    with state.session_factory() as session:
        kept = session.scalar(
            text(
                "select actions_json from room_messages where topic_id = :t "
                "and author_kind = 'agent' order by id desc limit 1"
            ),
            {"t": str(topic_id)},
        )
    assert kept[0]["kind"] == "steps"
    assert kept[0]["steps"][0]["tool"] == "search_memory"
    assert kept[0]["steps"][0]["status"] == "ok"
