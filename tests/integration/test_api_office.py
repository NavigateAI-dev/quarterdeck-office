"""The office: settings, sign-in, setup, status, ideas, calendars and the page."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Run as RunRow
from tests.integration.api_support import (
    NO_BEARER,
    OWNER_TOKEN,
    PASSWORD,
    PROVIDER_KEY,
    FakeIndexer,
    FakeRemote,
    FakeSlack,
    _action_path,
    _answers,
    _asked_last,
    _crossings,
    _finished,
    _hand_off,
    _line_with_project,
    _priced_filings,
    _proposed,
    _remote_stage,
    _said,
    _set_run,
    _slack_handler,
    _spend,
    _topic_with,
    _verified,
    _work_in,
    agent_with_bot,
    asked_in_slack,
    calendar_link_server,
    calendar_soon,
    calendar_today,
    company,
    granted_project,
    mac_with_calendars,
    new_agent,
    new_connection,
    new_schedule,
    open_session,
    password_sign_in,
    project_with_work,
    set_password,
    sign_in,
    slack_destination,
    stopped_run,
    store_of,
    venture_and_projects,
)

pytestmark = pytest.mark.integration


async def test_task_requires_an_existing_project(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/tasks", json={"project_id": str(uuid4()), "type": "analysis", "objective": "o"}
    )
    assert response.status_code == 404


async def test_the_interface_is_served(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert "Open your office" in page.text
    scripts = [line.split('"')[1] for line in page.text.splitlines() if "<script src=" in line]
    assert len(scripts) >= 8  # every screen's script, whatever the office has grown to
    for asset in ["/ui/styles.css", "/ui/office.css", *scripts]:
        assert (await client.get(asset)).status_code == 200, asset


async def test_the_task_room_offers_every_owner_decision(client: httpx.AsyncClient) -> None:
    room = (await client.get("/ui/office/tasks.js")).text
    for action in ("review-act:accept", "review-act:revise", "review-act:close"):
        assert action in room
    for decision in ("decide:approve", "decide:reject", "decide:revise"):
        assert decision in room
    assert "Nothing is executed or published without you" in room


async def test_the_review_panel_is_in_the_interface(client: httpx.AsyncClient) -> None:
    room = (await client.get("/ui/office/tasks.js")).text
    assert "This task needs your review" in room
    assert room.count("review") > 3


async def test_the_review_reset_reaches_the_checkpoint(client: httpx.AsyncClient) -> None:
    """A reset written only to the domain row is ignored on resume.

    The checkpoint is authoritative for continuation, so a resumed run would
    still see the spent revision budget and stop again immediately.
    """
    from app.orchestrator.review import ReviewStore

    run_id = await stopped_run(client)
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    override = ReviewStore(factory).start_cycle(UUID(run_id), "Focus on cost", "strategy", "Alex")
    assert override["revision_count"] == 0
    assert override["human_cycles"] == 1
    assert override["stopped_reason"] is None
    assert override["critique"]["revision_target"] == "strategy"
    assert override["critique"]["required_corrections"] == ["Focus on cost"]


async def test_accepting_sends_the_run_to_synthesis_not_back(
    client: httpx.AsyncClient,
) -> None:
    """The Critic advises; the owner decides whether the analysis is enough."""
    from app.orchestrator.review import ReviewStore

    run_id = await stopped_run(client)
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    override = ReviewStore(factory).start_cycle(
        UUID(run_id), "Good enough to proceed", None, "Alex"
    )
    assert override["critique"]["verdict"] == "PASS"
    assert override["critique"]["revision_target"] is None
    assert override["revision_count"] == 0


async def test_the_interface_offers_all_three_decisions(client: httpx.AsyncClient) -> None:
    room = (await client.get("/ui/office/tasks.js")).text
    for control in ("review-act:accept", "review-act:revise", "review-act:close"):
        assert control in room


async def test_a_connection_without_a_reference_starts_without_a_credential(
    client: httpx.AsyncClient,
) -> None:
    connection = await new_connection(client)
    assert connection["status"] == "CREDENTIAL_MISSING"
    assert connection["credential_configured"] is False
    assert "credential_ref" not in connection


async def test_a_new_agent_starts_with_an_enabled_first_version(
    client: httpx.AsyncClient,
) -> None:
    agent = await new_agent(client)
    assert agent["enabled_version"] == 1
    assert [v["version"] for v in agent["versions"]] == [1]
    assert agent["versions"][0]["instructions"] == "Cite sources."


async def test_a_session_cookie_is_http_only_and_strict(client: httpx.AsyncClient) -> None:
    response = await client.post("/session", json={"token": OWNER_TOKEN}, headers=NO_BEARER)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert OWNER_TOKEN not in response.text


async def test_a_wrong_token_opens_no_session(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/session", json={"token": "wrong-token-of-sufficient-length-xx"}, headers=NO_BEARER
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


async def test_a_reload_resumes_the_session(client: httpx.AsyncClient) -> None:
    csrf = await open_session(client)
    resumed = await client.get("/session", headers=NO_BEARER)
    assert resumed.status_code == 200
    assert resumed.json()["csrf_token"] == csrf


async def test_only_a_hash_of_the_session_is_stored(client: httpx.AsyncClient) -> None:
    from sqlalchemy import select

    from app.db.models import OwnerSession

    await open_session(client)
    raw = client.cookies.get("office_session")
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        stored = session.scalars(select(OwnerSession.token_hash)).all()
    assert raw and raw not in stored


async def test_repeated_wrong_tokens_are_throttled(client: httpx.AsyncClient) -> None:
    wrong = {"token": "wrong-token-of-sufficient-length-xx"}
    codes = [
        (await client.post("/session", json=wrong, headers=NO_BEARER)).status_code for _ in range(7)
    ]
    assert codes[:5] == [401] * 5
    assert codes[-1] == 429


async def test_a_foreign_page_cannot_sign_the_owner_in(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/session",
        json={"token": OWNER_TOKEN},
        headers={"Authorization": "", "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


async def test_a_one_time_code_signs_the_owner_in_once(client: httpx.AsyncClient) -> None:
    code = (await client.post("/session/code")).json()["code"]
    first = await client.post("/session", json={"code": code}, headers=NO_BEARER)
    assert first.status_code == 200
    assert "office_session" in first.headers["set-cookie"]
    again = await client.post("/session", json={"code": code}, headers=NO_BEARER)
    assert again.status_code == 401


async def test_an_expired_code_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.sessions as sessions

    code = (await client.post("/session/code")).json()["code"]
    real = sessions.time.monotonic
    monkeypatch.setattr(sessions.time, "monotonic", lambda: real() + sessions.CODE_TTL + 1)
    response = await client.post("/session", json={"code": code}, headers=NO_BEARER)
    assert response.status_code == 401


async def test_minting_a_code_needs_the_owner(client: httpx.AsyncClient) -> None:
    assert (await client.post("/session/code", headers=NO_BEARER)).status_code == 401


async def test_a_session_request_needs_exactly_one_credential(client: httpx.AsyncClient) -> None:
    both = await client.post(
        "/session", json={"token": OWNER_TOKEN, "code": "x" * 43}, headers=NO_BEARER
    )
    neither = await client.post("/session", json={}, headers=NO_BEARER)
    assert both.status_code == 422
    assert neither.status_code == 422


async def test_the_status_view_reports_health_without_secrets(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    await new_connection(client)
    response = await client.get("/status")
    assert response.status_code == 200
    body = response.json()
    from app.db.session import SCHEMA_REVISION

    assert body["database"]["revision"] == SCHEMA_REVISION
    assert body["sources_reader"]["state"] in {"ok", "stale", "not_seen"}
    assert body["worker"]["queued"] >= 0
    assert body["runner"]["state"] in {"ok", "stale", "not_seen"}
    assert body["credential_store"]["root_key"] == "present"
    assert body["connections"] == {"CREDENTIAL_MISSING": 1}
    assert body["storage"]["free_bytes"] > 0
    assert body["backups"]["latest_age_seconds"] is None
    assert "sk-" not in response.text and OWNER_TOKEN not in response.text


async def test_the_status_view_requires_the_owner(client: httpx.AsyncClient) -> None:
    assert (await client.get("/status", headers=NO_BEARER)).status_code == 401


async def test_retrieval_only_reaches_sources_granted_to_the_project(
    client: httpx.AsyncClient, indexer: FakeIndexer
) -> None:
    """A08/A15: one project's documents never reach another project's research."""
    from app.sources.retrieval import granted_passages

    mine, _ = await granted_project(client, indexer, "Mine", "Q3 revenue grew 12% in Europe.")
    other, _ = await granted_project(client, indexer, "Other", "Secret acquisition target: Acme.")
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        own = granted_passages(session, UUID(mine))
        theirs = granted_passages(session, UUID(other))
    assert any("revenue grew" in p.text for p in own)
    assert not any("Acme" in p.text for p in own)
    assert all(p.source == "Other" for p in theirs)


async def test_a_schedule_shows_its_next_runs_in_local_time(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    schedule, _ = await new_schedule(client)
    assert len(schedule["preview"]) == 3
    assert all(item.endswith(("CEST", "CET")) for item in schedule["preview"])
    assert schedule["enabled"] is True


async def test_unsubscribed_events_are_not_sent(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    await slack_destination(client, events=["failed"])
    state = client.app_state  # type: ignore[attr-defined]
    _, dispatch = await project_with_work(client, "Quiet")
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []


async def test_the_thread_it_was_asked_in_gets_the_answer_a_file_and_buttons(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels import slack_format
    from app.channels.notifications import deliver_pending, notify_run

    state, run_id = await asked_in_slack(client, monkeypatch)
    notify_run(state.session_factory, run_id, "COMPLETED", store_of(state))
    await deliver_pending(state.session_factory, state.worker.settings)
    by_channel = {channel: text for _, channel, text in fake_slack.sent}
    thread = by_channel["D1"]
    assert "✅ *Finished*" in thread and "Apply this quarter" in thread
    assert "Open your office" in by_channel["C1"], "a shared channel gets the line, not the work"
    assert "Apply this quarter" not in by_channel["C1"]
    origin_blocks = fake_slack.blocks[[c for _, c, _ in fake_slack.sent].index("D1")]
    assert origin_blocks[-1]["elements"][0]["action_id"] == slack_format.REWORK
    [(channel, in_thread, name, data)] = fake_slack.uploads
    assert (channel, in_thread) == ("D1", "1700.1")
    assert name.endswith("-v1.docx") and data[:2] == b"PK"


async def test_a_builds_thread_gets_the_build_not_a_document(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    state, run_id = await asked_in_slack(client, monkeypatch)
    state.repository.merge_state(run_id, {"build_folder": "site-1", "build_commit": "abc"})
    notify_run(state.session_factory, run_id, "COMPLETED", store_of(state))
    await deliver_pending(state.session_factory, state.worker.settings)
    thread = {channel: text for _, channel, text in fake_slack.sent}["D1"]
    assert "new version" in thread and "next version" in thread
    assert fake_slack.uploads == [], "a website is not sent as a Word file"


async def test_a_reworked_task_reports_in_its_thread_again(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.notifications import notify_run

    state, run_id = await asked_in_slack(client, monkeypatch)
    assert notify_run(state.session_factory, run_id, "COMPLETED", store_of(state)) == 2
    assert notify_run(state.session_factory, run_id, "COMPLETED", store_of(state)) == 0
    state.repository.add_event(run_id, "rework_requested", {"target": "research"})
    assert notify_run(state.session_factory, run_id, "COMPLETED", store_of(state)) == 2


async def test_progress_is_one_message_edited_as_the_steps_move(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels import slack_format
    from app.channels.progress import sync_progress

    state, run_id = await asked_in_slack(client, monkeypatch, status="RUNNING")
    settings = state.worker.settings
    state.repository.add_event(run_id, "step_started", {"node": "plan"})
    assert await sync_progress(state.session_factory, settings) == 1
    [(_, channel, text)] = fake_slack.sent
    assert channel == "D1" and fake_slack.threads == ["1700.1"]
    assert "*→ Planning*" in text
    assert fake_slack.blocks[0][-1]["elements"][0]["action_id"] == slack_format.STOP
    assert await sync_progress(state.session_factory, settings) == 0, "unchanged: no edit"
    state.repository.add_event(run_id, "step_completed", {"node": "plan"})
    state.repository.add_event(run_id, "step_started", {"node": "research"})
    assert await sync_progress(state.session_factory, settings) == 1
    [(_, ts, edited)] = fake_slack.updates
    assert ts == "1.1" and "✓ Planning" in edited and "*→ Research*" in edited
    with state.session_factory() as session, session.begin():
        row = session.get(RunRow, run_id)
        assert row is not None
        row.status = "COMPLETED"
        row.completed_at = datetime.now(UTC)
    await sync_progress(state.session_factory, settings)
    assert "Finished" in fake_slack.updates[-1][2]
    assert len(fake_slack.sent) == 1, "never a second progress message"


async def test_blocking_time_for_an_idea_plans_it_for_that_day(
    client: httpx.AsyncClient,
) -> None:
    """Ideas: a calendar event the owner added from an idea is remembered on
    it, and its day becomes the idea's day; only a new event counts."""
    from app.db.models import CalendarChange, CalendarSource

    state = client.app_state  # type: ignore[attr-defined]
    board = (await client.get("/ideas")).json()
    day = date.fromisoformat(board["today_is"]) + timedelta(days=2)
    starts = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)
    with state.session_factory() as session, session.begin():
        source = CalendarSource(name="Mac", kind="mac", provider="mac", secret_name="none")
        session.add(source)
        session.flush()
        made = CalendarChange(
            source_id=source.id,
            action="create",
            payload_json={"title": "Print button", "starts_at": starts.isoformat()},
        )
        moved = CalendarChange(source_id=source.id, action="update", payload_json={})
        session.add_all([made, moved])
        session.flush()
        made_id, moved_id = made.id, moved.id
    idea = (await client.post("/ideas", json={"text": "Print button"})).json()
    wrong = await client.put(f"/ideas/{idea['id']}/block", json={"change_id": moved_id})
    assert wrong.status_code == 409
    blocked = (await client.put(f"/ideas/{idea['id']}/block", json={"change_id": made_id})).json()
    assert blocked["blocked_at"].startswith(starts.isoformat()[:16])
    assert blocked["plan_for"] == starts.date().isoformat() and blocked["bucket"] == "later"


async def test_ideas_can_be_reworded_by_hand(client: httpx.AsyncClient) -> None:
    idea = (await client.post("/ideas", json={"text": "boat??"})).json()
    said = await client.put(f"/ideas/{idea['id']}/text", json={"text": "Look at boats"})
    assert said.json()["text"] == "Look at boats"


async def test_a_watch_starts_only_on_the_press_and_checks_for_free_until_it_changes(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP6: unchanged means no model call; changed means one call, and a told
    change goes to the chosen channel once; the cost counts, the cap holds."""
    # No DNS in a test: these hosts count as public, the office's own never.
    monkeypatch.setattr("app.browser.safety.address_public", lambda host: host != "localhost")
    from types import SimpleNamespace

    from app import watches
    from app.db.models import ChannelDelivery, ChannelDestination, WatchCheck
    from app.office_settings import spent_this_month

    state = client.app_state  # type: ignore[attr-defined]
    await slack_destination(client)
    with state.session_factory() as session:
        channel = session.scalars(select(ChannelDestination)).first().id
    body = {
        "kind": "page",
        "target": "https://shop.example.com/kettle",
        "condition": "",
        "every_minutes": 60,
        "post_to_id": str(channel),
        "daily_cap_usd": 0.02,
    }
    assert (await client.post("/watches", json=body)).status_code == 422, "the press first"
    bad = await client.post("/watches", json={**body, "target": "file:///etc", "confirm": True})
    assert bad.status_code == 422
    made = await client.post("/watches", json={**body, "title": "Kettle", "confirm": True})
    assert made.status_code == 201, made.text
    watch_id = UUID(made.json()["id"])

    page = {"text": "Kettle\nPrice: €59\nIn stock"}
    calls: list[Any] = []

    async def read(address: str) -> str:
        return page["text"]

    async def complete(messages: list[dict[str, str]]) -> Any:
        calls.append(messages)
        return SimpleNamespace(
            content='{"tell": true, "message": "Now €49 (was €59).", "done": false}',
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=30),
            estimated_cost=0.012,
        )

    async def check(at: datetime) -> str:
        return await watches.check(state.session_factory, watch_id, at, read, None, complete)

    # Early in the day, so every check below falls on the same day.
    now = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0)
    assert await check(now) == "baseline" and calls == []
    assert await check(now + timedelta(hours=1)) == "same" and calls == [], "free"
    page["text"] = "Kettle\nPrice: €49\nIn stock"
    assert await check(now + timedelta(hours=2)) == "told"
    assert "-Price: €59" in calls[0][1]["content"] and "In stock" not in calls[0][1]["content"]
    with state.session_factory() as session:
        sent = session.scalars(
            select(ChannelDelivery).where(ChannelDelivery.event_kind == "watch")
        ).all()
        assert [d.text for d in sent] == [
            "👀 Kettle: Now €49 (was €59). (https://shop.example.com/kettle)"
        ]
        assert spent_this_month(session) >= 0.012
    page["text"] = "Kettle\nPrice: €45\nIn stock"
    assert await check(now + timedelta(hours=3)) == "capped", "over its daily cap"
    assert len(calls) == 1
    listed = (await client.get("/watches")).json()["watches"][0]
    assert listed["last_said"] == "Now €49 (was €59)." and listed["status"] == "active"
    assert (await client.post(f"/watches/{watch_id}/pause")).json()["status"] == "paused"
    assert (await client.post(f"/watches/{watch_id}/delete")).json()["status"] == "deleted"
    assert (await client.get("/watches")).json()["watches"] == []
    with state.session_factory() as session:
        assert session.scalars(select(WatchCheck)).first() is not None, "its costs stay"


async def test_a_watch_with_a_condition_ends_when_it_is_met(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA5: "tell me if it will rain before 15:00": the first look is judged,
    and a met condition ends the watch; failing reads stop it, saying why."""
    monkeypatch.setattr("app.browser.safety.address_public", lambda host: host != "localhost")
    from types import SimpleNamespace

    from app import watches
    from app.db.models import Watch

    state = client.app_state  # type: ignore[attr-defined]
    made = await client.post(
        "/watches",
        json={
            "kind": "page",
            "target": "https://www.buienradar.nl/weer/leiden",
            "condition": "it will rain in Lyon before 15:00",
            "every_minutes": 60,
            "confirm": True,
        },
    )
    watch_id = UUID(made.json()["id"])

    async def read(address: str) -> str:
        return "Lyon: rain from 14:20"

    async def complete(messages: list[dict[str, str]]) -> Any:
        assert "rain in Lyon before 15:00" in messages[1]["content"]
        return SimpleNamespace(
            content='{"tell": true, "message": "Rain from 14:20 in Lyon.", "done": true}',
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10),
            estimated_cost=0.001,
        )

    now = datetime.now(UTC)
    told = await watches.check(state.session_factory, watch_id, now, read, None, complete)
    assert told == "told"
    with state.session_factory() as session:
        assert session.get(Watch, watch_id).status == "ended"

    other = UUID(
        (
            await client.post(
                "/watches",
                json={"kind": "search", "target": "kettle prices", "confirm": True},
            )
        ).json()["id"]
    )
    for n in range(watches.FAILURES_TO_STOP):
        await watches.check(
            state.session_factory, other, now + timedelta(hours=n), None, None, None
        )
    with state.session_factory() as session:
        stopped = session.get(Watch, other)
        assert stopped.status == "stopped" and "search_not_configured" in stopped.stopped_reason


async def test_an_idea_kept_on_a_phone_says_so(client: httpx.AsyncClient) -> None:
    kept = await client.post("/ideas", json={"text": "Call the printer", "source": "phone"})
    assert kept.json()["source"] == "phone"
    refused = await client.post("/ideas", json={"text": "x", "source": "slack"})
    assert refused.status_code == 422, "only the office's own screens say where"


async def test_chat_usage_is_recorded_per_bot_and_day(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.inbound_office import SlackOffice

    agent = await agent_with_bot(client)
    state = client.app_state  # type: ignore[attr-defined]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(agent["bot"]["installation"]["app_id"]),
    )
    entry = office.agent_entry()
    assert entry is not None and entry["profile_id"] == agent["id"]
    office._record(entry, 300, 0.01)
    office._record(entry, 200, None)
    assert office._tokens_today() == 500
    facts = office.facts(entry)
    assert facts["recent_runs"] == []  # it has taken part in no runs yet


async def test_the_overview_needs_the_owner(client: httpx.AsyncClient) -> None:
    response = await client.get("/office/overview", headers={"Authorization": ""})
    assert response.status_code == 401


def test_the_reader_rejects_staged_paths_outside_its_own_directory(tmp_path: Path) -> None:
    from app.sources.reader import index_root

    staging = tmp_path / "staged"
    (staging / "job1").mkdir(parents=True)
    (staging / "job1" / "a.txt").write_text("hello")
    assert index_root(staging, "job1")["documents"][0]["path"] == "a.txt"
    assert index_root(staging, "../secrets")["error"] == "root_outside_mount"
    assert index_root(staging, "/etc")["error"] == "root_outside_mount"


def test_a_role_with_its_own_agent_keeps_that_agents_model() -> None:
    """The office's role default fills gaps; it never overrides an agent."""
    from app.agent_routing import apply_role_models

    class Role:
        def __init__(self, role: str) -> None:
            self.role = role
            self.gateway = "office-gateway"
            self.provider = ""
            self.model_alias = ""

    from app.orchestrator.graph import AgentSet

    agents = AgentSet(
        planner=Role("chief"),  # type: ignore[arg-type]
        researcher=Role("researcher"),  # type: ignore[arg-type]
        strategist=Role("strategist"),  # type: ignore[arg-type]
        critic=Role("critic"),  # type: ignore[arg-type]
        chief=Role("chief"),  # type: ignore[arg-type]
    )
    entries = {
        "researcher": {
            "connection_id": "c1",
            "provider": "groq",
            "model_id": "m",
            "base_url": None,
        },
        "critic": {"connection_id": "c2", "provider": "groq", "model_id": "m", "base_url": None},
    }
    apply_role_models(
        agents, entries, lambda entry: f"completer:{entry['connection_id']}", {"critic"}
    )
    assert agents.researcher.gateway == "completer:c1"
    assert agents.critic.gateway == "office-gateway"  # its own agent decides


async def test_the_board_needs_the_owner(client: httpx.AsyncClient) -> None:
    assert (await client.get("/office/board", headers={"Authorization": ""})).status_code == 401


async def test_setup_is_owner_only_and_carries_no_secret(client: httpx.AsyncClient) -> None:
    assert (await client.get("/setup", headers={"Authorization": ""})).status_code == 401
    body = (await client.get("/setup")).text
    assert PROVIDER_KEY not in body and "credential_ref" not in body


async def test_a_message_the_platform_never_received_is_sent_once(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run, reconcile_unknown

    await slack_destination(client)
    state = client.app_state  # type: ignore[attr-defined]
    _, dispatch = await project_with_work(client, "Never arrived")
    fake_slack.fail_with = "outcome_unknown"
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")
    await deliver_pending(state.session_factory, state.worker.settings)
    fake_slack.fail_with = None
    fake_slack.history = []  # the platform holds no such message
    settled = await reconcile_unknown(state.session_factory, state.worker.settings)
    assert settled["not_delivered"] == 1
    await deliver_pending(state.session_factory, state.worker.settings)
    assert len(fake_slack.sent) == 1  # exactly once, never twice
    await deliver_pending(state.session_factory, state.worker.settings)
    assert len(fake_slack.sent) == 1


def test_the_downgrade_moves_every_role_once_and_says_so() -> None:
    """The swap happens at a step boundary, and is recorded for the owner."""
    from app.config import get_settings
    from app.orchestrator.graph import AgentSet
    from app.orchestrator.runner import Dispatch
    from app.runtime.job import Job
    from app.runtime.worker import RunWorker

    class Role:
        def __init__(self) -> None:
            self.gateway = "office-model"
            self.provider = ""
            self.model_alias = ""
            self.role = "researcher"

    class Repository:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def usage(self, run_id: Any) -> dict[str, Any]:
            return {"spent": {"cost_usd": 4.0, "cost_reported": True}}

        def add_event(self, run_id: Any, kind: str, payload: dict[str, Any]) -> None:
            self.events.append((kind, payload))

    entry = {
        "connection_id": str(uuid4()),
        "provider": "groq",
        "model_id": "llama-3.3-70b",
        "base_url": None,
    }
    repository = Repository()

    class NoSession:
        """The rule and the model are stubbed, so no session is used."""

        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

    worker = RunWorker(get_settings(), repository, None, session_factory=NoSession)  # type: ignore[arg-type]
    agents = AgentSet(
        planner=Role(), researcher=Role(), strategist=Role(), critic=Role(), chief=Role()
    )  # type: ignore[arg-type]
    job = Job(
        dispatch=Dispatch(run_id=uuid4(), task_id=uuid4(), project_id=uuid4()),
        objective="o",
        context={},
    )
    import app.runtime.model_stack as worker_module

    original_rule = worker_module.downgrade_rule
    original_completer = worker_module.completer_for_entry
    original_backups = worker_module.provider_backups
    worker_module.downgrade_rule = lambda session, project_id: (2.5, entry)  # type: ignore[assignment]
    worker_module.completer_for_entry = lambda entry, settings, factory: "cheap-model"  # type: ignore[assignment]
    worker_module.provider_backups = lambda session: {}  # type: ignore[assignment]
    try:
        before_step = worker._downgrade_watch(job, agents)
        assert before_step is not None
        before_step("research")
        before_step("strategy")  # a second boundary must not move it again
    finally:
        worker_module.downgrade_rule = original_rule  # type: ignore[assignment]
        worker_module.completer_for_entry = original_completer  # type: ignore[assignment]
        worker_module.provider_backups = original_backups  # type: ignore[assignment]
    assert agents.researcher.gateway == "cheap-model"
    assert agents.chief.model_alias.startswith("connection:")
    assert [kind for kind, _ in repository.events] == ["model_downgraded"]
    assert repository.events[0][1]["model"] == "llama-3.3-70b"


async def test_a_paused_schedule_skips_instead_of_failing(client: httpx.AsyncClient) -> None:
    from app.office_settings import OfficePaused, paused

    state = client.app_state  # type: ignore[attr-defined]
    await client.put("/settings", json={"pause_schedules": True})
    with state.session_factory() as session:
        assert paused(session, state.worker.settings, "schedules") is True
        assert paused(session, state.worker.settings, "chat") is False
    # The scheduler treats it as a skip, not a fault of the schedule.
    from app.scheduler import OfficePaused as SchedulerPaused

    assert SchedulerPaused is OfficePaused
    await client.put("/settings", json={"pause_schedules": False})


async def test_chat_says_the_office_is_paused_rather_than_starting_work() -> None:
    from app.channels.inbound_office import SlackOffice

    class Paused(SlackOffice):
        def __init__(self) -> None:  # noqa: D107 - a stand-in for one check
            pass

        def paused_for_chat(self) -> bool:
            return True

        def agent_entry(self) -> None:
            return None

    answer = await Paused().converse([], "what is happening?")
    assert answer is not None and "switched off" in answer


async def test_a_ventures_doctrine_reaches_every_task_in_it(client: httpx.AsyncClient) -> None:
    """The point of the tier: the same agents, judged differently per business."""
    from app.db.models import Project as ProjectModel
    from app.project_context import build_run_context

    venture_id, inside, _, outside = await venture_and_projects(client)
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    with factory() as session:
        within = build_run_context(session, session.get(ProjectModel, UUID(inside)), settings)
        without = build_run_context(session, session.get(ProjectModel, UUID(outside)), settings)
    assert "primary instrument by article" in within["doctrine"]
    assert within["venture"].startswith("Fintech")
    # A project belonging to no venture is exactly as it was before ventures.
    assert without["doctrine"] == "" and without["venture"] == ""
    assert venture_id


async def test_a_paused_office_refuses_the_start_and_leaves_the_button(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, reply, submitted = await _proposed(client, monkeypatch)
    await client.put("/settings", json={"pause_new_tasks": True})
    try:
        refused = await client.post(_action_path(project_id, reply), json={})
    finally:
        await client.put("/settings", json={"pause_new_tasks": False})
    assert refused.status_code == 409 and refused.json()["detail"] == "office_paused"
    assert submitted == []
    assert "run_id" not in (await _said(client, project_id))[-1]["actions"][0]


async def test_one_bots_threads_are_not_anothers(client: httpx.AsyncClient) -> None:
    from tests.unit.test_inbound import Office, request

    state = client.app_state  # type: ignore[attr-defined]
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    await _slack_handler(state, office, uuid4()).handle(request("Prepare a brief", thread="t1"))
    other = Office(owner="U_OWNER")
    other.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    await _slack_handler(state, other, uuid4()).handle(request("yes", event="e2", thread="t1"))
    assert other.started == []


async def test_the_office_suggests_a_title_while_the_ask_is_typed(
    client: httpx.AsyncClient,
) -> None:
    from app.titles import title_for

    ask = "Please research what the Wwft requires of an EMI, so that we can plan onboarding."
    suggested = await client.post("/titles", json={"objective": ask})
    assert suggested.status_code == 200
    assert suggested.json() == {"title": title_for(ask)}
    assert (await client.post("/titles", json={"objective": ""})).status_code == 422


async def test_a_paid_call_is_spending_the_task_and_the_project_both_count(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.budget import Budget, RunLedger
    from app.policies.store import PolicyGate
    from app.project_settings import spent_this_month

    state = client.app_state  # type: ignore[attr-defined]
    _, project = await _priced_filings(client, monkeypatch, 0.35)
    sent: list[str] = []
    _answers(monkeypatch, sent)
    dispatch = state.repository.create(UUID(project["id"]), "look it up")
    ledger = RunLedger(Budget(max_tokens=100_000, max_cost_usd=5.0), 1000)
    tools = await state.worker._mcp_tools(dispatch, PolicyGate(state.session_factory), ledger)
    assert tools is not None
    server_name = tools.servers[0].name

    assert await tools.call(server_name, "search", {"q": "wwft"}) == "a filing"
    assert sent == ["search"]
    usage = state.repository.usage(dispatch.run_id)
    assert usage["spent"]["cost_usd"] == pytest.approx(0.35)
    assert [row["role"] for row in usage["by_role"]] == ["tools"]
    assert ledger.spent_cost == pytest.approx(0.35)
    with state.session_factory() as session:
        assert spent_this_month(session, UUID(project["id"])) == pytest.approx(0.35)


async def test_a_question_asked_again_counts_once(client: httpx.AsyncClient) -> None:
    from app.artifacts import ArtifactStore
    from app.related_work import recent_answers

    state = client.app_state  # type: ignore[attr-defined]
    tag = uuid4().hex[:6]
    older = await _finished(client, f"Rerun {tag}", f"KYC vendors {tag}", f"First try {tag}")
    newer = state.repository.create(older.project_id, f"KYC vendors {tag}")
    state.repository.record_stage(newer, "synthesis", {"recommendation": f"Second try {tag}"})
    from sqlalchemy import update as sql_update

    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(RunRow)
            .where(RunRow.id == newer.run_id)
            .values(status="COMPLETED", completed_at=datetime.now(UTC))
        )
    with state.session_factory() as session:
        found = recent_answers(session, ArtifactStore(state.worker.settings.workspace_root), set())
    mine = [item for item in found if tag in item.text]
    assert [item.run_id for item in mine] == [newer.run_id]


def test_a_rework_note_from_nothing_still_asks_for_better() -> None:
    from app.api.routes import rework_note

    assert "more useful to act on" in rework_note({})


async def test_a_task_near_its_own_budget_is_announced_by_its_title(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending

    state = client.app_state  # type: ignore[attr-defined]
    settings = client.settings  # type: ignore[attr-defined]
    await slack_destination(client, ["limit_warning", "completed"])
    _, dispatch = await project_with_work(client, "Budgeted")
    _set_run(state, dispatch.run_id, status="RUNNING", budget_json={"max_tokens": 1000})
    _spend(state, dispatch, 0.01, tokens=1000)
    _crossings(state, settings)
    await deliver_pending(state.session_factory, state.worker.settings)
    said = [text for _, _, text in fake_slack.sent]
    assert len(said) == 2, "80% and 100% are two warnings"
    assert all("Budgeted objective" in text for text in said)
    assert any("used all of its" in text for text in said)


async def test_limit_warnings_are_not_sent_where_nobody_asked(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending

    state = client.app_state  # type: ignore[attr-defined]
    settings = client.settings  # type: ignore[attr-defined]
    await slack_destination(client, ["completed"])
    await client.put("/settings", json={"monthly_cost_limit_usd": 1.0})
    _, dispatch = await project_with_work(client, "Quiet")
    _spend(state, dispatch, 0.95)
    _crossings(state, settings)
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []
    await client.put("/settings", json={"monthly_cost_limit_usd": None})


async def test_what_the_office_can_reach_is_checked_from_where_it_reads(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RE1: the fetch service's probe, plus search and tool servers; kept a while."""
    from app import office_reach

    asked: list[int] = []

    async def probe(self: Any) -> list[dict[str, Any]]:
        asked.append(1)
        return [
            {
                "id": "youtube",
                "name": "YouTube",
                "what": "w",
                "host": "youtube.com",
                "state": "warn",
                "via": "yt-dlp",
                "detail": "Sign in to confirm",
                "fix": "f",
            }
        ]

    monkeypatch.setattr("app.tools.fetch_client.ExchangeFetcher.probe", probe)
    exchange = client.settings.workspace_root / "exchange"  # type: ignore[attr-defined]
    monkeypatch.setattr(client.settings, "fetch_exchange", exchange)  # type: ignore[attr-defined]
    office_reach.CACHE.clear()
    first = await client.get("/office/reach")
    assert first.status_code == 200, first.text
    ids = [row["id"] for row in first.json()["sources"]]
    assert ids == ["search", "youtube", "tools"]
    assert first.json()["sources"][0]["state"] == "off", "the fixture office has no search provider"
    await client.get("/office/reach")
    assert len(asked) == 1, "kept for a while: each check calls out to every platform"
    await client.get("/office/reach", params={"fresh": "true"})
    assert len(asked) == 2


async def test_how_many_tasks_run_at_once_is_the_owners_to_set(
    client: httpx.AsyncClient,
) -> None:
    view = (await client.get("/settings")).json()
    assert view["settings"]["runs_at_once"] == view["configured"]["runs_at_once"]
    assert set(view["worker"]) == {"running", "waiting"}
    saved = (await client.put("/settings", json={"runs_at_once": 5})).json()
    assert saved["settings"]["runs_at_once"] == 5
    assert (await client.put("/settings", json={"runs_at_once": 9})).status_code == 422
    cleared = (await client.put("/settings", json={"runs_at_once": None})).json()
    assert cleared["settings"]["runs_at_once"] == view["configured"]["runs_at_once"]


async def test_needs_you_holds_only_what_waits_on_the_owner(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Compare the prices.")
    waiting = await _work_in(client, topic["id"], "NEEDS_HUMAN_REVIEW")
    await _hand_off(client, topic["id"], "Draft the offer.")
    finished = await _work_in(client, topic["id"], "COMPLETED")
    desk = (await client.get("/office/desk")).json()
    # The desk is the whole office's: other work may be waiting there too.
    needs = {n["run_id"]: n for n in desk["needs"]}
    assert str(waiting) in needs and str(finished) not in needs
    need = needs[str(waiting)]
    assert need["kind"] == "review"
    assert need["from"] == {"kind": "topic", "id": topic["id"], "title": topic["title"]}
    assert need["what"] and need["need"], "what happened, and what is needed"


async def test_the_desk_shows_deliveries_to_glance_at(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Compare the prices.")
    run_id = await _work_in(client, topic["id"], "COMPLETED")
    described = state.repository.describe([run_id])[0][0]
    state.repository.record_stage(
        described, "synthesis", {"recommendation": "Three of five publish prices."}
    )
    delivered = (await client.get("/office/desk")).json()["delivered"]
    mine = [d for d in delivered if d["run_id"] == str(run_id)]
    assert mine and "Three of five publish prices." in mine[0]["concluded"]
    assert mine[0]["from"]["id"] == topic["id"]


async def test_a_thought_kept_on_the_floor_is_planned_for_a_day(
    client: httpx.AsyncClient,
) -> None:
    """ID1, ID2: kept as written, for free; planned for a day, someday, or done."""
    from datetime import date as day_of

    assert (await client.post("/ideas", json={"text": "   "})).status_code == 422
    kept = await client.post("/ideas", json={"text": "  Add a print button  "})
    assert kept.status_code == 201, kept.text
    idea = kept.json()
    assert (idea["text"], idea["source"], idea["bucket"]) == ("Add a print button", "floor", "new")
    board = (await client.get("/ideas")).json()
    today = day_of.fromisoformat(board["today_is"])
    assert [i["id"] for i in board["new"]] == [idea["id"]]

    async def plan(**body: Any) -> httpx.Response:
        return await client.put(f"/ideas/{idea['id']}/plan", json=body)

    assert (await plan(day=today.isoformat())).json()["bucket"] == "today"
    later = (today + timedelta(days=3)).isoformat()
    assert (await plan(day=later)).json()["bucket"] == "later"
    assert (await client.get("/ideas")).json()["later"][0]["plan_for"] == later
    past = await plan(day=(today - timedelta(days=1)).isoformat())
    assert past.status_code == 409 and past.json()["detail"] == "in_the_past"
    assert (await plan(someday=True)).json()["bucket"] == "someday"
    assert (await plan(day=None)).json()["bucket"] == "new", "back to unsorted"

    done = await client.post(f"/ideas/{idea['id']}/finish", json={"status": "done"})
    assert done.json()["status"] == "done"
    assert (await client.get("/ideas")).json()["counts"] == {
        "today": 0,
        "new": 0,
        "later": 0,
        "someday": 0,
    }
    again = await client.post(f"/ideas/{idea['id']}/finish", json={"status": "dropped"})
    assert again.status_code == 409
    assert (await plan(day=later)).status_code == 409, "a finished idea is not planned"


async def test_an_overdue_idea_stays_on_top_and_agents_read_todays(
    client: httpx.AsyncClient,
) -> None:
    """ID2: a passed day keeps the idea under Today, marked; agents read only
    what is planned for today or before, never the unsorted ones."""
    from sqlalchemy import update as sql_update

    from app import widgets
    from app.db.models import Idea

    state = client.app_state  # type: ignore[attr-defined]
    ids = {}
    for words in ("Call the printer", "Ask Mira about Q4", "Unsorted thought"):
        ids[words] = (await client.post("/ideas", json={"text": words})).json()["id"]
    today = date.fromisoformat((await client.get("/ideas")).json()["today_is"])
    await client.put(f"/ideas/{ids['Ask Mira about Q4']}/plan", json={"day": today.isoformat()})
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(Idea)
            .where(Idea.id == UUID(ids["Call the printer"]))
            .values(plan_for=today - timedelta(days=2))
        )
    board = (await client.get("/ideas")).json()
    assert [(i["text"], i["bucket"]) for i in board["today"]] == [
        ("Call the printer", "overdue"),
        ("Ask Mira about Q4", "today"),
    ]
    with state.session_factory() as session:
        told = "\n".join(widgets.agent_context(session, datetime.now(UTC), "task"))
    assert "Ask Mira about Q4" in told and "Call the printer (planned for" in told
    assert "Unsorted thought" not in told


# Access hardening.


async def test_wrong_sign_ins_never_lock_the_owner_out(client: httpx.AsyncClient) -> None:
    """R14: five wrong guesses used to block every sign-in for a minute, the
    owner's included; a device on the home network could keep them out."""
    anonymous = {"Authorization": ""}
    for _ in range(7):
        await client.post("/session", json={"code": "not-a-real-code"}, headers=anonymous)
    slowed = await client.post("/session", json={"code": "still-wrong"}, headers=anonymous)
    assert slowed.status_code == 429, "wrong guesses are still slowed"
    owner = client.headers["Authorization"].removeprefix("Bearer ")
    signed_in = await client.post("/session", json={"token": owner}, headers=anonymous)
    assert signed_in.status_code == 200, "the right credential always gets in"


async def test_a_session_over_the_tailscale_address_gets_a_secure_cookie(
    client: httpx.AsyncClient,
) -> None:
    """R13: the session cookie was never Secure, even over Tailscale's HTTPS."""
    from datetime import UTC, datetime

    from app.db.models import AccessPoint

    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        point = session.get(AccessPoint, "remote") or AccessPoint(kind="remote")
        point.address = "https://office.example-tailnet.ts.net"
        point.enabled = True
        point.verified_at = datetime.now(UTC)
        session.merge(point)
    owner = client.headers["Authorization"].removeprefix("Bearer ")
    anonymous = {"Authorization": ""}
    remote = await client.post(
        "/session",
        json={"token": owner},
        headers={**anonymous, "Host": "office.example-tailnet.ts.net"},
    )
    assert remote.status_code == 200, remote.text
    assert "secure" in remote.headers["set-cookie"].lower()
    local = await client.post(
        "/session", json={"token": owner}, headers={**anonymous, "Host": "127.0.0.1:8000"}
    )
    assert "secure" not in local.headers["set-cookie"].lower()


async def test_related_work_stays_within_its_business_line(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update as sql_update

    from app.artifacts import ArtifactStore
    from app.related_work import recent_answers

    state = client.app_state  # type: ignore[attr-defined]
    tag = uuid4().hex[:6]
    _, mine = await _line_with_project(client, "Harbor AI", "Pricing")
    _, theirs = await _line_with_project(client, "Client work", "Acme")
    for project_id, answer in ((mine, f"Ours {tag}"), (theirs, f"Client's {tag}")):
        dispatch = state.repository.create(UUID(project_id), f"Question {answer}")
        state.repository.record_stage(dispatch, "synthesis", {"recommendation": answer})
        with state.session_factory() as session, session.begin():
            session.execute(
                sql_update(RunRow)
                .where(RunRow.id == dispatch.run_id)
                .values(status="COMPLETED", completed_at=datetime.now(UTC))
            )
    store = ArtifactStore(state.worker.settings.workspace_root)
    with state.session_factory() as session:
        found = [f.text for f in recent_answers(session, store, set(), UUID(mine))]
    assert any(f"Ours {tag}" in t for t in found)
    assert not any(f"Client's {tag}" in t for t in found), "another line's work is not mixed in"
    await client.put("/settings", json={"related_across_lines": True})
    with state.session_factory() as session:
        found = [f.text for f in recent_answers(session, store, set(), UUID(mine))]
    assert any(f"Client's {tag}" in t for t in found), "unless the owner allows it"


async def test_an_unknown_host_is_refused_and_a_verified_one_served(
    client: httpx.AsyncClient,
) -> None:
    refused = await client.get("/health/live", headers={"Host": "evil.example.com"})
    assert refused.status_code == 421
    assert (
        await client.get("/health/live", headers={"Host": "192.168.1.20:8000"})
    ).status_code == 421
    _verified(client, "home", "http://192.168.1.20:8000")
    served = await client.get("/health/live", headers={"Host": "192.168.1.20:8000"})
    assert served.status_code == 200
    assert (await client.get("/health/live", headers={"Host": "127.0.0.1:8000"})).status_code == 200


async def test_a_phone_signs_in_once_with_a_pairing_code(client: httpx.AsyncClient) -> None:
    from app.db.models import AccessPoint

    state = client.app_state  # type: ignore[attr-defined]
    not_ready = await client.post("/access/pair", json={"kind": "remote"})
    assert not_ready.status_code == 409
    _verified(client, "remote", "https://quarterdeck.tail1234.ts.net")
    paired = (await client.post("/access/pair", json={"kind": "remote"})).json()
    assert paired["url"].startswith("https://quarterdeck.tail1234.ts.net/#code=")
    code = paired["url"].split("#code=")[1]
    assert "<svg" in paired["qr"] and int(paired["expires_in"]) <= 120
    phone = {
        "Host": "quarterdeck.tail1234.ts.net",
        "Origin": "https://quarterdeck.tail1234.ts.net",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) Safari/604.1",
        "Authorization": "",
    }
    opened = await client.post("/session", json={"code": code}, headers=phone)
    assert opened.status_code == 200, opened.text
    with state.session_factory() as session:
        found = session.get(AccessPoint, "remote")
        assert found is not None and found.confirmed_at is not None
        assert found.confirmed_device == "iPhone · Safari"
    again = await client.post("/session", json={"code": code}, headers=phone)
    assert again.status_code == 401, "a pairing code works once"


# The home network.


async def test_the_home_network_needs_its_one_time_setup_first(
    client: httpx.AsyncClient,
) -> None:
    home = (await client.get("/access")).json()["points"][0]
    assert home["kind"] == "home" and home["setup"]["required"] is True
    refused = await client.post("/access/home/enable", json={"address": "http://192.168.1.50:8000"})
    assert refused.status_code == 409 and refused.json()["detail"] == "setup_required"


async def test_the_home_network_turns_on_only_once_verified(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.settings, "office_bind", "0.0.0.0")  # type: ignore[attr-defined]  # noqa: S104
    monkeypatch.setattr(
        client.settings,  # type: ignore[attr-defined]
        "office_lan_addresses",
        "http://192.168.1.50:8000,http://mac-mini.local:8000",
    )
    home = (await client.get("/access")).json()["points"][0]
    assert home["setup"]["required"] is False
    assert home["setup"]["suggested"] == ["http://192.168.1.50:8000", "http://mac-mini.local:8000"]
    bad = await client.post("/access/home/enable", json={"address": "http://127.0.0.1:8000"})
    assert bad.status_code == 422 and "this computer" in bad.json()["detail"]

    answered = {"ok": False}

    async def probe(address: str, host: str | None = None) -> dict[str, Any]:
        return {"answered": answered["ok"], "via": address, "host": host}

    monkeypatch.setattr("app.access.probe", probe)
    failed = await client.post("/access/home/enable", json={"address": "http://192.168.1.50:8000"})
    assert failed.status_code == 200
    assert failed.json()["enabled"] is False and failed.json()["check"]["answered"] is False
    lan = {"Host": "192.168.1.50:8000"}
    assert (await client.get("/health/live", headers=lan)).status_code == 421
    answered["ok"] = True
    on = (
        await client.post("/access/home/enable", json={"address": "http://192.168.1.50:8000"})
    ).json()
    assert on["enabled"] is True and on["verified_at"] and on["check"]["answered"] is True
    assert (await client.get("/health/live", headers=lan)).status_code == 200
    off = (await client.post("/access/home/disable")).json()
    assert off["enabled"] is False
    assert (await client.get("/health/live", headers=lan)).status_code == 421


async def test_the_probe_answers_under_any_host_and_names_this_office(
    client: httpx.AsyncClient,
) -> None:
    from app import access

    seen = await client.get("/access/probe/abc123", headers={"Host": "192.168.1.50:8000"})
    assert seen.status_code == 200
    assert seen.json() == {"office": access.OFFICE_ID, "nonce": "abc123"}


async def test_remote_access_needs_its_container_then_a_login(client: httpx.AsyncClient) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    state.remote = None
    assert _remote_stage((await client.get("/access")).json()) == "setup_required"
    remote = FakeRemote()
    state.remote = remote
    assert _remote_stage((await client.get("/access")).json()) == "setup_required"
    remote.status = {"backend": "NeedsLogin", "auth_url": "", "routed": False}
    remote.answers["connect"] = {
        "ok": True,
        "backend": "NeedsLogin",
        "auth_url": "https://login.tailscale.com/a/abc",
    }
    asked = await client.post("/access/remote/connect", json={})
    assert asked.status_code == 200
    assert asked.json()["remote"]["stage"] == "awaiting_authorization"
    assert asked.json()["remote"]["auth_url"] == "https://login.tailscale.com/a/abc"


async def test_an_auth_key_is_passed_once_and_never_shown(client: httpx.AsyncClient) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    remote = FakeRemote()
    remote.status = {"backend": "NeedsLogin", "auth_url": "", "routed": False}
    remote.answers["connect"] = {
        "ok": True,
        "backend": "Running",
        "dns_name": "quarterdeck.t.ts.net",
    }
    state.remote = remote
    key = "tskey-auth-k123-SECRETSECRET"
    answered = await client.post("/access/remote/connect", json={"auth_key": key})
    assert answered.status_code == 200
    assert remote.asked == [("connect", {"auth_key": key})]
    assert key not in answered.text
    assert key not in (await client.get("/access")).text


async def test_remote_access_turns_on_only_when_serve_routes_to_the_office(
    client: httpx.AsyncClient,
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    remote = FakeRemote()
    remote.status = {
        "backend": "Running",
        "dns_name": "quarterdeck.tail1234.ts.net",
        "routed": False,
        "https": False,
    }
    state.remote = remote
    assert _remote_stage((await client.get("/access")).json()) == "off"
    remote.answers["route_on"] = {
        "ok": False,
        "needs_https": True,
        "url": "https://login.tailscale.com/f/serve?node=x",
        "routed": False,
    }
    refused = (await client.post("/access/remote/enable")).json()
    assert refused["enabled"] is False
    assert refused["check"]["needs_https"] is True
    phone = {"Host": "quarterdeck.tail1234.ts.net"}
    assert (await client.get("/health/live", headers=phone)).status_code == 421
    remote.answers["route_on"] = {"ok": True, "routed": True, "https": True}
    on = (await client.post("/access/remote/enable")).json()
    assert on["enabled"] is True and on["address"] == "https://quarterdeck.tail1234.ts.net"
    assert on["remote"]["stage"] == "ready_for_phone_test"
    assert (await client.get("/health/live", headers=phone)).status_code == 200
    assert all("funnel" not in action for action, _ in remote.asked)

    remote.answers["route_off"] = {"ok": True, "routed": False}
    off = (await client.post("/access/remote/disable")).json()
    assert off["enabled"] is False and off["remote"]["stage"] == "off"
    assert ("route_off", {}) in remote.asked
    assert (await client.get("/health/live", headers=phone)).status_code == 421
    assert (await client.get("/health/live")).status_code == 200, "local access stays"


async def test_a_route_that_will_not_stop_is_reported_not_hidden(
    client: httpx.AsyncClient,
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    remote = FakeRemote()
    remote.status = {"backend": "Running", "dns_name": "quarterdeck.t.ts.net", "routed": True}
    state.remote = remote
    _verified(client, "remote", "https://quarterdeck.t.ts.net")
    remote.answers["route_off"] = {"ok": False, "routed": True}
    off = (await client.post("/access/remote/disable")).json()
    assert off["enabled"] is False, "the office refuses the address either way"
    assert off["remote"]["stage"] == "connection_problem"
    assert off["check"]["still_routed"] is True


async def test_the_login_link_is_shown_only_while_waiting_for_approval(
    client: httpx.AsyncClient,
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    remote = FakeRemote()
    remote.status = {
        "backend": "Running",
        "auth_url": "https://login.tailscale.com/a/old",
        "dns_name": "quarterdeck.t.ts.net",
        "routed": False,
    }
    state.remote = remote
    shown = (await client.get("/access")).json()["points"][1]["remote"]
    assert shown["stage"] == "off" and shown["auth_url"] == ""


async def test_an_agent_reads_the_answer_of_this_discussions_work_even_unfinished(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    topic, fake, judge = await _topic_with(client, monkeypatch, "do")
    await _hand_off(client, topic["id"], "Plan a six-day hiking route in the Pyrenees.")
    run_id = await _work_in(client, topic["id"], "CANCELLED")
    described = state.repository.describe([run_id])[0][0]
    state.repository.record_stage(
        described,
        "strategy",
        {"recommendation": "Day 1: PR 1.2 Vereda do Areeiro; Day 2: PR 6 Levada das 25 Fontes."},
    )
    judge.kind = "talk"
    prompt = await _asked_last(client, topic["id"], fake, "Which of these routes suit children?")
    assert "Vereda do Areeiro" in prompt and "Levada das 25 Fontes" in prompt
    assert "CANCELLED" in prompt, "and that it did not finish"


async def test_an_agent_knows_its_colleagues(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, _ = await _topic_with(client, monkeypatch, "talk")
    prompt = await _asked_last(client, topic["id"], fake, "What are each of your strengths?")
    for name in ("Iris", "Cato", "Vera", "Wren"):
        assert name in prompt
    assert "Your colleagues" in prompt


async def test_a_calendar_link_connects_and_shows_on_the_owners_day(
    client: httpx.AsyncClient,
) -> None:
    """CAL1, 7 Oct: the owner's calendars, read-only; the link is never shown again."""
    calendar_link_server(client, calendar_today("Client call"))
    added = await client.post(
        "/calendars",
        json={
            "kind": "ics",
            "provider": "google",
            "name": "Work",
            "url": "https://calendar.example.com/private-abc/basic.ics",
        },
    )
    assert added.status_code == 201, added.text
    assert "private-abc" not in added.text
    listed = (await client.get("/calendars")).json()
    assert [s["name"] for s in listed["sources"]] == ["Work"]
    assert listed["preferences"] == {"share": "titles", "hold": True, "prep": True}
    assert "private-abc" not in json.dumps(listed)
    day = (await client.get("/calendars/day")).json()
    assert [e["title"] for e in day["events"]] == ["Client call"]
    assert day["status"]["state"] == "busy" and day["connected"] == 1
    source = added.json()["id"]
    assert (await client.delete(f"/calendars/{source}")).status_code == 204
    assert (await client.get("/calendars")).json()["sources"] == []


async def test_a_calendar_that_cannot_be_read_is_refused_with_why(
    client: httpx.AsyncClient,
) -> None:
    calendar_link_server(client, "", status=401)
    refused = await client.post(
        "/calendars",
        json={"kind": "ics", "provider": "outlook", "url": "https://x.example/c.ics"},
    )
    assert refused.status_code == 422 and refused.json()["detail"] == "calendar_refused"
    incomplete = await client.post(
        "/calendars", json={"kind": "caldav", "provider": "icloud", "username": "me@icloud.com"}
    )
    assert incomplete.json()["detail"] == "calendar_account_incomplete"


async def test_the_owner_chooses_what_agents_see_of_the_calendar(
    client: httpx.AsyncClient,
) -> None:
    saved = await client.put("/calendars/preferences", json={"share": "busy", "hold": False})
    assert saved.json() == {"share": "busy", "hold": False, "prep": True}
    wrong = await client.put("/calendars/preferences", json={"share": "everything"})
    assert wrong.status_code == 422


async def test_a_declined_prep_brief_is_not_offered_again(client: httpx.AsyncClient) -> None:
    calendar_link_server(client, calendar_soon("Board review"))
    await client.post(
        "/calendars", json={"kind": "ics", "provider": "google", "url": "https://c.example/a.ics"}
    )
    offer = (await client.get("/calendars/day")).json()["prep"]
    assert (await client.post(f"/calendars/prep/{offer['id']}/dismiss")).status_code == 204
    assert (await client.get("/calendars/day")).json()["prep"] is None
    await client.put("/calendars/preferences", json={"prep": False})


async def test_the_mac_helper_asks_macos_only_after_the_owners_press(
    client: httpx.AsyncClient,
) -> None:
    """CAL5, 7 Oct: no password; macOS's own dialog, and only once asked for."""
    idle = (await client.get("/calendars/mac/helper")).json()
    assert idle == {"wanted": False, "ask": False, "sync": False, "changes": False}, (
        "nothing asks unprompted"
    )
    pressed = await client.post("/calendars/mac")
    assert pressed.status_code == 201 and pressed.json()["name"] == "This Mac"
    poll = (await client.get("/calendars/mac/helper")).json()
    assert poll == {"wanted": True, "ask": True, "sync": False, "changes": False}
    [source] = (await client.get("/calendars")).json()["sources"]
    assert source["helper"] == "running", "it just asked"
    denied = await client.post("/calendars/mac/helper", json={"access": "denied"})
    assert denied.json()["last_error"] == "mac_access_denied"
    assert (await client.get("/calendars/mac/helper")).json()["ask"] is False


async def test_the_mac_helper_brings_the_calendar_apps_events_in(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/calendars/mac")
    now = datetime.now(UTC)
    report = {
        "access": "granted",
        "calendars": [{"name": "Home", "account": "iCloud"}, {"name": "Work", "account": "Google"}],
        "events": [
            {
                "uid": "E1@1",
                "calendar": "Home",
                "title": "Dentist",
                "starts_at": (now - timedelta(minutes=5)).isoformat(),
                "ends_at": (now + timedelta(minutes=40)).isoformat(),
                "description": "Bring card 777",
            }
        ],
    }
    reported = (await client.post("/calendars/mac/helper", json=report)).json()
    assert reported["event_count"] == 1 and reported["calendars"] == ["Home", "Work"]
    day = (await client.get("/calendars/day")).json()
    assert [e["title"] for e in day["events"]] == ["Dentist"]
    assert day["status"]["state"] == "busy"
    source = reported["id"]
    await client.post(f"/calendars/{source}/sync")
    assert (await client.get("/calendars/mac/helper")).json()["sync"] is True
    assert (await client.delete(f"/calendars/{source}")).status_code == 204
    assert (await client.get("/calendars/mac/helper")).json()["wanted"] is False


async def test_sign_in_with_google_connects_on_the_owners_consent(
    client: httpx.AsyncClient,
) -> None:
    """CAL6, 7 Oct: Google's own consent page; no link or password pasted."""
    fake = company(client)
    missing = await client.post(
        "/calendars/oauth/google/start", json={"origin": "http://127.0.0.1:8000"}
    )
    assert missing.json()["detail"] == "sign_in_app_missing"
    saved = await client.put(
        "/calendars/apps/google", json={"client_id": "cid.apps", "client_secret": "csecret"}
    )
    assert saved.json() == {"google": True, "microsoft": False}
    back = await sign_in(client, "google")
    assert back.status_code == 303
    assert back.headers["location"] == "http://127.0.0.1:8000/#calendars/connected"
    form = fake.forms[0]
    assert form["grant_type"] == ["authorization_code"] and form["code_verifier"]
    listed = (await client.get("/calendars")).json()
    [source] = listed["sources"]
    assert source["name"] == "Google" and source["username"] == "me@gmail.com"
    assert source["event_count"] == 1
    assert "rt-1" not in json.dumps(listed) and "csecret" not in json.dumps(listed)
    day = await client.get(
        "/calendars/day",
        params={
            "start": datetime.now(UTC).isoformat(),
            "end": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        },
    )
    assert [e["title"] for e in day.json()["events"]] == ["Investor call"]


async def test_sign_in_with_microsoft_needs_only_the_app_id(client: httpx.AsyncClient) -> None:
    """CAL7: a public client, no secret; the Graph calendar view in UTC."""
    fake = company(client)
    await client.put("/calendars/apps/microsoft", json={"client_id": "app-guid"})
    back = await sign_in(client, "microsoft")
    assert back.headers["location"].endswith("#calendars/connected")
    assert "client_secret" not in fake.forms[0]
    [source] = (await client.get("/calendars")).json()["sources"]
    assert source["username"] == "me@outlook.com" and source["event_count"] == 1


async def test_a_declined_or_stale_sign_in_says_so(client: httpx.AsyncClient) -> None:
    company(client)
    await client.put("/calendars/apps/microsoft", json={"client_id": "app-guid"})
    started = await client.post(
        "/calendars/oauth/microsoft/start", json={"origin": "http://127.0.0.1:8000"}
    )
    state = parse_qs(urlparse(started.json()["url"]).query)["state"][0]
    declined = await client.get(
        f"/calendars/oauth/callback?state={state}&error=access_denied", follow_redirects=False
    )
    assert declined.headers["location"].endswith("#calendars/sign_in_declined")
    stale = await client.get(
        f"/calendars/oauth/callback?state={state}&code=x", follow_redirects=False
    )
    assert stale.headers["location"] == "/#calendars/sign_in_expired", "a state works once"
    assert (await client.get("/calendars")).json()["sources"] == []


async def test_the_owner_edits_a_mac_calendar_through_the_helper(
    client: httpx.AsyncClient,
) -> None:
    """CAL9, 7 Oct: the owner's edit is queued, made by the helper, then read back."""
    await mac_with_calendars(client)
    span = {
        "start": datetime.now(UTC).isoformat(),
        "end": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    events = {
        e["title"]: e for e in (await client.get("/calendars/day", params=span)).json()["events"]
    }
    assert events["Dentist"]["writable"] is True and events["Dentist"]["color"] == "#3366cc"
    assert events["Bank holiday"]["writable"] is False
    writable = (await client.get("/calendars/writable")).json()
    assert [c["name"] for c in writable] == ["Home"]
    start = datetime.now(UTC) + timedelta(days=1)
    new = {
        "calendar_id": "home",
        "title": "Gym",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
    }
    queued = await client.post("/calendars/events", json=new)
    assert queued.status_code == 202 and queued.json()["status"] == "pending"
    assert (await client.get("/calendars/mac/helper")).json()["changes"] is True
    [change] = (await client.get("/calendars/mac/helper/changes")).json()
    assert change["action"] == "create" and change["payload"]["title"] == "Gym"
    done = await client.post(f"/calendars/mac/helper/changes/{change['id']}", json={"ok": True})
    assert done.json()["status"] == "done"
    assert (await client.get(f"/calendars/changes/{change['id']}")).json()["status"] == "done"
    assert (await client.get("/calendars/mac/helper")).json()["changes"] is False
    moved = await client.put(
        f"/calendars/events/{events['Dentist']['id']}", json=new | {"title": "Dentist (moved)"}
    )
    assert moved.json()["payload"]["uid"] == "A@1"
    removed = await client.delete(f"/calendars/events/{events['Dentist']['id']}")
    assert removed.json()["action"] == "delete"


async def test_a_calendar_the_owner_cannot_change_refuses_edits(
    client: httpx.AsyncClient,
) -> None:
    await mac_with_calendars(client)
    start = datetime.now(UTC) + timedelta(days=1)
    into_holidays = {
        "calendar_id": "holidays",
        "title": "x",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
    }
    refused = await client.post("/calendars/events", json=into_holidays)
    assert refused.status_code == 409 and refused.json()["detail"] == "calendar_read_only"
    span = {
        "start": datetime.now(UTC).isoformat(),
        "end": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    events = {
        e["title"]: e for e in (await client.get("/calendars/day", params=span)).json()["events"]
    }
    holiday = await client.delete(f"/calendars/events/{events['Bank holiday']['id']}")
    assert holiday.status_code == 409
    backwards = await client.post(
        "/calendars/events",
        json=into_holidays
        | {"calendar_id": "home", "ends_at": (start - timedelta(hours=1)).isoformat()},
    )
    assert backwards.status_code == 422, "an event ends after it starts"


async def test_an_unknown_time_zone_is_refused_or_falls_back_to_utc(
    client: httpx.AsyncClient,
) -> None:
    wrong = await client.put("/calendars/preferences", json={"timezone": "Not a zone!"})
    assert wrong.status_code == 422
    from app.calendars.store import preferences, save_preferences

    with client.app_state.session_factory() as session, session.begin():  # type: ignore[attr-defined]
        save_preferences(session, {"timezone": "Mars/Olympus"})
        assert preferences(session)["timezone"] == "UTC"


async def test_the_owner_edits_a_google_calendar_through_the_sign_in(
    client: httpx.AsyncClient,
) -> None:
    """CAL10, 7 Oct: the change is made by the office through Google, at once."""
    fake = company(
        client,
        scope="https://www.googleapis.com/auth/calendar.readonly "
        "https://www.googleapis.com/auth/calendar.events",
    )
    await client.put("/calendars/apps/google", json={"client_id": "cid", "client_secret": "s"})
    await client.put("/calendars/preferences", json={"timezone": "Europe/Amsterdam"})
    await sign_in(client, "google")
    writable = (await client.get("/calendars/writable")).json()
    assert [c["id"] for c in writable] == ["me@gmail.com"], "a calendar Google lets them change"
    start = datetime(2026, 10, 20, 8, tzinfo=UTC)
    made = await client.post(
        "/calendars/events",
        json={
            "calendar_id": "me@gmail.com",
            "title": "Focus",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=2)).isoformat(),
        },
    )
    assert made.json()["status"] == "done", made.text
    method, url, body = fake.writes[-1]
    assert method == "POST" and url.endswith("/calendars/me%40gmail.com/events")
    assert body["summary"] == "Focus" and body["start"]["timeZone"] == "Europe/Amsterdam"
    span = {
        "start": datetime.now(UTC).isoformat(),
        "end": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
    }
    [event] = (await client.get("/calendars/day", params=span)).json()["events"]
    assert event["writable"] is True and event["color"] == "#1a73e8"
    removed = await client.delete(f"/calendars/events/{event['id']}")
    assert removed.json()["status"] == "done"
    assert fake.writes[-1][0] == "DELETE" and fake.writes[-1][1].endswith("/events/e1")


async def test_a_microsoft_change_patches_the_event(client: httpx.AsyncClient) -> None:
    fake = company(client, scope="Calendars.ReadWrite User.Read offline_access")
    await client.put("/calendars/apps/microsoft", json={"client_id": "app-guid"})
    await sign_in(client, "microsoft")
    span = {
        "start": datetime.now(UTC).isoformat(),
        "end": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
    }
    [event] = (await client.get("/calendars/day", params=span)).json()["events"]
    moved = await client.put(
        f"/calendars/events/{event['id']}",
        json={
            "title": "Standup (moved)",
            "starts_at": event["starts_at"],
            "ends_at": event["ends_at"],
        },
    )
    assert moved.json()["status"] == "done", moved.text
    method, url, body = fake.writes[-1]
    assert method == "PATCH" and url == "https://graph.microsoft.com/v1.0/me/events/m1"
    assert body["subject"] == "Standup (moved)"


async def test_a_sign_in_that_allowed_reading_only_refuses_changes(
    client: httpx.AsyncClient,
) -> None:
    company(client, scope="https://www.googleapis.com/auth/calendar.readonly")
    await client.put("/calendars/apps/google", json={"client_id": "cid", "client_secret": "s"})
    await sign_in(client, "google")
    assert (await client.get("/calendars/writable")).json() == []
    start = datetime(2026, 10, 20, 8, tzinfo=UTC)
    refused = await client.post(
        "/calendars/events",
        json={
            "calendar_id": "me@gmail.com",
            "title": "x",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
        },
    )
    assert refused.status_code == 409 and refused.json()["detail"] == "calendar_read_only"


async def test_signing_in_again_renews_the_connection_rather_than_copying_it(
    client: httpx.AsyncClient,
) -> None:
    """CAL10: a sign-in from before allowed reading only; signing in again allows changes."""
    fake = company(client, scope="https://www.googleapis.com/auth/calendar.readonly")
    await client.put("/calendars/apps/google", json={"client_id": "cid", "client_secret": "s"})
    await sign_in(client, "google")
    assert (await client.get("/calendars/writable")).json() == []
    fake.scope = (
        "https://www.googleapis.com/auth/calendar.readonly "
        "https://www.googleapis.com/auth/calendar.events"
    )
    await sign_in(client, "google")
    assert len((await client.get("/calendars")).json()["sources"]) == 1
    assert [c["id"] for c in (await client.get("/calendars/writable")).json()] == ["me@gmail.com"]


async def test_a_mac_that_suddenly_shows_no_calendars_keeps_the_last_good_copy(
    client: httpx.AsyncClient,
) -> None:
    """8 Oct: the owner's calendar came up empty after a night."""
    reported = await mac_with_calendars(client)
    assert reported["event_count"] == 2
    empty = (
        await client.post(
            "/calendars/mac/helper", json={"access": "granted", "calendars": [], "events": []}
        )
    ).json()
    assert empty["event_count"] == 2 and len(empty["calendars"]) == 2


async def test_no_password_is_offered_until_one_is_set(client: httpx.AsyncClient) -> None:
    assert (await client.get("/session/methods", headers=NO_BEARER)).json() == {"password": False}
    assert (await password_sign_in(client)).status_code == 401


async def test_a_wrong_password_or_name_opens_no_session(client: httpx.AsyncClient) -> None:
    await set_password(client)
    wrong = await password_sign_in(client, password="not the password")
    assert wrong.status_code == 401
    assert "set-cookie" not in wrong.headers
    assert (await password_sign_in(client, name="someone")).status_code == 401


async def test_a_short_password_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.put("/settings/login", json={"name": "Alex", "password": "short"})
    assert response.status_code == 422
    assert response.json()["detail"] == "password_too_short"


async def test_changing_the_password_needs_the_current_one(client: httpx.AsyncClient) -> None:
    await set_password(client)
    refused = await client.put(
        "/settings/login", json={"name": "Alex", "password": "another long password"}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "current_password_wrong"
    changed = await client.put(
        "/settings/login",
        json={
            "name": "Alex",
            "password": "another long password",
            "current_password": PASSWORD,
        },
    )
    assert changed.status_code == 200
    assert (await password_sign_in(client)).status_code == 401
    assert (await password_sign_in(client, password="another long password")).status_code == 200


async def test_turning_password_sign_in_off(client: httpx.AsyncClient) -> None:
    await set_password(client)
    wrong = await client.post("/settings/login/remove", json={"current_password": "nope"})
    assert wrong.status_code == 422
    off = await client.post("/settings/login/remove", json={"current_password": PASSWORD})
    assert off.json()["set"] is False
    assert (await password_sign_in(client)).status_code == 401


async def test_guessing_stops_even_the_right_password(client: httpx.AsyncClient) -> None:
    await set_password(client)
    for _ in range(5):
        assert (await password_sign_in(client, password="a wrong guess")).status_code == 401
    blocked = await password_sign_in(client)
    assert blocked.status_code == 429
    assert "set-cookie" not in blocked.headers
    # The owner key is counted apart and still opens the office.
    assert (
        await client.post("/session", json={"token": OWNER_TOKEN}, headers=NO_BEARER)
    ).status_code == 200


async def test_settings_for_the_password_need_the_owner(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/settings/login",
        json={"name": "Alex", "password": PASSWORD},
        headers=NO_BEARER,
    )
    assert response.status_code == 401


async def test_the_wall_is_read_and_changed_by_the_owner(client: httpx.AsyncClient) -> None:
    wall = (await client.get("/widgets")).json()["widgets"]
    assert {w["kind"] for w in wall} >= {"calendar", "month"}
    off = await client.post("/widgets/month/remove", json={"clean": False})
    assert off.status_code == 200 and off.json()["placed"] is False
    on = await client.post("/widgets/month/place")
    assert on.json()["placed"] is True
    read = await client.patch("/widgets/month", json={"agents_read": True})
    assert read.json()["agents_read"] is True
    assert (await client.post("/widgets/nothing/place")).status_code == 404
    assert (await client.get("/widgets", headers=NO_BEARER)).status_code == 401


# ---- Accounts ----


async def test_accounts_lists_google_microsoft_and_services(client: httpx.AsyncClient) -> None:
    page = (await client.get("/accounts")).json()
    ids = [row["id"] for row in page["connections"]]
    assert ids[:2] == ["google", "microsoft"]
    assert {"slack", "notion", "linear"} <= set(ids)
    google = page["connections"][0]
    assert google["app_set"] is False and google["server"] is None
    assert "gmail_read" in google["tools"]
    assert any(row["id"] == "stripe" for row in page["keys"])
    assert "secret" not in json.dumps(page).lower()


async def test_connecting_google_needs_its_sign_in_app_first(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/accounts/google/connect", json={"origin": "http://localhost:8000"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "sign_in_app_missing"
    assert (
        await client.post("/accounts/dropbox/connect", json={"origin": "http://localhost:8000"})
    ).status_code == 404


async def test_the_owner_chooses_the_cli_that_codes_on_the_mac(client: httpx.AsyncClient) -> None:
    shown = (await client.get("/settings/coding-agent")).json()
    assert shown["agent"] == "claude_code"
    assert [a["id"] for a in shown["agents"]] == ["claude_code", "codex", "opencode", "dsh"]
    # Nothing is found until the helper on the Mac says what it has.
    assert not any(a["installed"] for a in shown["agents"])
    await client.post(
        "/coder/hello",
        json={"clis": [{"id": "codex", "path": "/x/codex", "version": "1", "signed_in": True}]},
    )
    found = (await client.get("/settings/coding-agent")).json()
    codex = next(a for a in found["agents"] if a["id"] == "codex")
    assert (codex["installed"], codex["ready"], codex["runs"]) == (True, True, True)
    chosen = await client.put("/settings/coding-agent", json={"agent": "codex"})
    assert chosen.status_code == 200 and chosen.json()["agent"] == "codex"
    unknown = await client.put("/settings/coding-agent", json={"agent": "cursor"})
    assert unknown.status_code == 422
    # The old key form is gone: a key is refused, not stored.
    keyed = await client.put("/settings/coding-agent", json={"claude_key": "sk-ant-xxxxxxxxxx"})
    assert keyed.status_code == 422


async def test_every_response_names_the_interface_it_belongs_to(client: httpx.AsyncClient) -> None:
    """UP1: a page kept open across an update can tell it is out of date."""
    first = (await client.get("/health/live")).headers["x-office-build"]
    page = (await client.get("/")).headers["x-office-build"]
    assert first == page and len(first) == 12


def test_the_fingerprint_changes_with_the_page_code(tmp_path: Path) -> None:
    from app.main import interface_fingerprint

    (tmp_path / "office").mkdir()
    (tmp_path / "office" / "core.js").write_text("a")
    before = interface_fingerprint(tmp_path)
    (tmp_path / "office" / "core.js").write_text("b")
    assert interface_fingerprint(tmp_path) != before


async def _waiting_for_approval(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, **risk: Any
) -> tuple[Any, UUID, Any, str]:
    """A Slack task waiting for approval of a proposal, low risk unless told otherwise."""
    from app.approvals import ApprovalScope

    state, run_id = await asked_in_slack(client, monkeypatch, status="WAITING_FOR_APPROVAL")
    with state.session_factory() as session:
        run = session.get(RunRow, run_id)
        assert run is not None
        project_id, task_id = run.project_id, run.root_task_id
    scope = ApprovalScope(
        run_id=run_id,
        project_id=project_id,
        objective="Pick a CRM",
        recommendation="Take CRM B for five seats",
        proposed_engineering_scope="None",
        **risk,
    )
    approval_id = state.approvals.open(run_id, task_id, scope)
    return state, run_id, approval_id, scope.digest()


def _slack_office(state: Any) -> Any:
    from app.channels.inbound_office import SlackOffice

    return SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        uuid4(),
    )


async def test_a_low_risk_approval_card_can_be_approved_once_from_slack(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels import slack_format
    from app.channels.notifications import deliver_pending, notify_run
    from app.db.models import Approval, Decision

    state, run_id, approval_id, digest = await _waiting_for_approval(client, monkeypatch)
    notify_run(state.session_factory, run_id, "WAITING_FOR_APPROVAL", store_of(state))
    await deliver_pending(state.session_factory, state.worker.settings)
    channels = [channel for _, channel, _ in fake_slack.sent]
    card = fake_slack.blocks[channels.index("D1")]
    approve, reject = card[-1]["elements"][:2]
    assert (approve["action_id"], reject["action_id"]) == (
        slack_format.APPROVE,
        slack_format.REJECT,
    )
    assert approve["value"] == f"{approval_id}:{digest}"
    text = {channel: said for _, channel, said in fake_slack.sent}["D1"]
    assert "Low: records a decision only" in text and "Take CRM B" in text

    office = _slack_office(state)
    stale = await office.decide(slack_format.APPROVE, f"{approval_id}:{'0' * 64}")
    assert stale == (False, slack_format_refusal("stale_scope"))
    assert await office.decide(slack_format.APPROVE, approve["value"]) == (
        True,
        "✓ Approved by you in Slack.",
    )
    again = await office.decide(slack_format.REJECT, approve["value"])
    assert again == (False, "That approval was already decided.")
    with state.session_factory() as session:
        row = session.get(Approval, approval_id)
        assert row is not None and row.status == "APPROVED"
        assert row.resolved_by == "owner (Slack)"
        decision = session.get(Decision, row.decision_id)
        assert decision is not None and decision.decision == "Take CRM B for five seats"


def slack_format_refusal(code: str) -> str:
    from app.channels.approving import DECIDED

    return DECIDED[code]


async def test_an_approval_that_runs_code_is_only_approved_in_the_office(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels import slack_format
    from app.channels.notifications import deliver_pending, notify_run
    from app.db.models import Approval

    state, run_id, approval_id, digest = await _waiting_for_approval(
        client, monkeypatch, executes_code=True, repository="acme/site"
    )
    notify_run(state.session_factory, run_id, "WAITING_FOR_APPROVAL", store_of(state))
    await deliver_pending(state.session_factory, state.worker.settings)
    channels = [channel for _, channel, _ in fake_slack.sent]
    card = fake_slack.blocks[channels.index("D1")]
    pressed = (
        [b.get("action_id") for b in card[-1]["elements"]] if card[-1]["type"] == "actions" else []
    )
    assert slack_format.APPROVE not in pressed and slack_format.REJECT not in pressed
    text = {channel: said for _, channel, said in fake_slack.sent}["D1"]
    assert "Higher: runs code" in text
    # A button made up by hand is refused all the same: the stored proposal decides.
    decided, said = await _slack_office(state).decide(
        slack_format.APPROVE, f"{approval_id}:{digest}"
    )
    assert not decided and "in the office" in said
    with state.session_factory() as session:
        row = session.get(Approval, approval_id)
        assert row is not None and row.status == "PENDING"


async def test_the_task_card_says_how_long_and_what_it_has_cost(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.progress import sync_progress
    from app.db.models import UsageEvent

    state, run_id = await asked_in_slack(client, monkeypatch, status="RUNNING")
    with state.session_factory() as session, session.begin():
        run = session.get(RunRow, run_id)
        assert run is not None
        run.started_at = datetime.now(UTC) - timedelta(minutes=12, seconds=5)
        run.budget_json = {**(run.budget_json or {}), "max_cost_usd": 2.0}
        session.add(
            UsageEvent(
                run_id=run_id,
                project_id=run.project_id,
                role="researcher",
                provider="anthropic",
                model_alias="m",
                status="ok",
                prompt_tokens=1,
                completion_tokens=1,
                estimated_cost=0.38,
                attempts=1,
                latency_seconds=1.0,
                occurred_at=datetime.now(UTC),
            )
        )
    state.repository.add_event(run_id, "step_started", {"node": "plan"})
    await sync_progress(state.session_factory, state.worker.settings)
    [(_, _, text)] = fake_slack.sent
    assert "Time: 12 min" in text and "Cost: $0.38 of $2.00" in text
    assert "Running · step 1" in text and "Agent task" in text
    fields = next(b for b in fake_slack.blocks[0] if "fields" in b)["fields"]
    assert [f["text"] for f in fields] == ["*Time*\n12 min", "*Cost*\n$0.38 of $2.00"]
