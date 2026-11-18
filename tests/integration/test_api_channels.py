"""Chat channels: Slack and deliveries."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.models import ChannelDelivery as ChannelDeliveryRow
from app.db.models import Run as RunRow
from tests.integration.api_support import (
    BOT,
    DASHBOARD,
    FakeSlack,
    LookupModel,
    _crossings,
    _slack_handler,
    _spend,
    agent_with_bot,
    asked_in_slack,
    finished_run,
    new_schedule,
    project_with_work,
    slack_destination,
    store_of,
)

pytestmark = pytest.mark.integration


async def test_a_bot_token_is_verified_stored_encrypted_and_never_returned(
    client: httpx.AsyncClient, fake_slack: FakeSlack, tmp_path: Path
) -> None:
    response = await client.post("/channels/slack/installations", json={"bot_token": BOT})
    assert response.status_code == 201
    body = response.json()
    assert body["workspace_name"] == "Northwind"
    assert BOT not in response.text
    assert BOT not in (await client.get("/channels")).text
    on_disk = "".join(p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file())
    assert BOT not in on_disk


async def test_an_invalid_bot_token_is_refused(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    response = await client.post(
        "/channels/slack/installations", json={"bot_token": "not-a-token-at-all"}
    )
    assert response.status_code == 422 or response.json()["detail"] == "invalid_auth"


async def test_available_channels_are_listed_for_choosing(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    installation = (
        await client.post("/channels/slack/installations", json={"bot_token": BOT})
    ).json()
    channels = (await client.get(f"/channels/installations/{installation['id']}/available")).json()
    assert [(c["external_id"], c["can_post"]) for c in channels] == [("C1", True), ("C2", False)]


async def test_oauth_install_uses_a_single_use_state(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    await client.put(
        "/channels/slack/app",
        json={
            "client_id": "123.456",
            "client_secret": "client-secret-fixture-value",
            "redirect_uri": "https://office.example.com/channels/slack/oauth/callback",
        },
    )
    url = (await client.get("/channels/slack/install")).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    bad = await client.get(
        "/channels/slack/oauth/callback",
        params={"code": "c", "state": "forged" * 8},
        headers={"Authorization": ""},
    )
    assert bad.status_code == 400
    ok = await client.get(
        "/channels/slack/oauth/callback",
        params={"code": "c", "state": state},
        headers={"Authorization": ""},
    )
    assert ok.status_code == 200
    assert "Northwind" in ok.text and "xoxb-oauth-token-fixture" not in ok.text
    again = await client.get(
        "/channels/slack/oauth/callback",
        params={"code": "c", "state": state},
        headers={"Authorization": ""},
    )
    assert again.status_code == 400
    [installation] = (await client.get("/channels")).json()["installations"]
    assert installation["owner_external_id"] == "U0OWNER"


async def test_the_app_secret_is_never_returned(client: httpx.AsyncClient) -> None:
    await client.put(
        "/channels/slack/app",
        json={"client_id": "123.456", "client_secret": "client-secret-fixture-value"},
    )
    listing = (await client.get("/channels")).text
    assert "client-secret-fixture-value" not in listing
    assert '"client_secret_configured":true' in listing.replace(" ", "")


async def test_oauth_needs_an_https_redirect(client: httpx.AsyncClient) -> None:
    await client.put(
        "/channels/slack/app",
        json={
            "client_id": "1.2",
            "client_secret": "client-secret-fixture-value",
            "redirect_uri": "http://127.0.0.1:8000/channels/slack/oauth/callback",
        },
    )
    response = await client.get("/channels/slack/install")
    assert response.status_code == 409
    assert response.json()["detail"] == "redirect_must_be_https"


async def test_a_run_needing_approval_notifies_once_without_content(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    await slack_destination(client)
    state = client.app_state  # type: ignore[attr-defined]
    project, dispatch = await project_with_work(client, "Notified")
    notify_run(state.session_factory, dispatch.run_id, "WAITING_FOR_APPROVAL")
    notify_run(state.session_factory, dispatch.run_id, "WAITING_FOR_APPROVAL")  # replayed
    await deliver_pending(state.session_factory, state.worker.settings)
    assert len(fake_slack.sent) == 1
    token, channel, text = fake_slack.sent[0]
    assert (token, channel) == (BOT, "C1")
    assert "needs your approval" in text
    assert "Notified rec" not in text  # no artifact content leaves the office
    [delivery] = (
        await client.get("/channels/deliveries", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert delivery["status"] == "SENT"


async def test_an_unknown_delivery_outcome_is_not_retried_automatically(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    await slack_destination(client)
    state = client.app_state  # type: ignore[attr-defined]
    _, dispatch = await project_with_work(client, "Unknown")
    fake_slack.fail_with = "outcome_unknown"
    notify_run(state.session_factory, dispatch.run_id, "FAILED")
    await deliver_pending(state.session_factory, state.worker.settings)
    fake_slack.fail_with = None
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []
    [delivery] = (
        await client.get("/channels/deliveries", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert delivery["status"] == "UNKNOWN"


async def test_a_failed_delivery_is_retried_then_given_up(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    await slack_destination(client)
    state = client.app_state  # type: ignore[attr-defined]
    _, dispatch = await project_with_work(client, "Flaky")
    fake_slack.fail_with = "unreachable"
    notify_run(state.session_factory, dispatch.run_id, "FAILED")
    for _ in range(5):
        await deliver_pending(state.session_factory, state.worker.settings)
    [delivery] = (
        await client.get("/channels/deliveries", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert delivery["status"] == "FAILED"
    assert delivery["attempts"] == 3


async def test_a_test_message_needs_explicit_confirmation(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    destination = await slack_destination(client)
    refused = await client.post(
        f"/channels/destinations/{destination}/test", json={"confirm": False}
    )
    assert refused.status_code == 409
    sent = await client.post(f"/channels/destinations/{destination}/test", json={"confirm": True})
    assert sent.status_code == 200
    assert len(fake_slack.sent) == 1


async def test_removing_an_installation_deletes_its_token(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.credentials import open_secret_store

    installation = (
        await client.post("/channels/slack/installations", json={"bot_token": BOT})
    ).json()
    assert (await client.delete(f"/channels/installations/{installation['id']}")).status_code == 204
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    assert not open_secret_store(settings).exists(f"channel-{installation['id']}")
    assert (await client.get("/channels")).json()["installations"] == []


async def test_the_owner_paired_once_is_known_to_every_bot_in_the_workspace(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.members import set_owner

    await client.post("/channels/slack/installations", json={"bot_token": BOT})
    agent = await agent_with_bot(client)
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        set_owner(session, "slack", "T0TEAM", "U0OWNER")
    bot = (await client.get(f"/agents/{agent['id']}/chat-bot/slack")).json()
    assert bot["installation"]["owner_external_id"] == "U0OWNER"
    with state.session_factory() as session, session.begin():
        set_owner(session, "slack", "T0TEAM", "U0NEWOWNER")  # re-pairing replaces it
    [office] = (await client.get("/channels")).json()["installations"]
    assert office["owner_external_id"] == "U0NEWOWNER"


async def test_a_dashboard_reaches_slack_with_its_charts_and_its_page(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SL5: charts as pictures in the thread, and the page linked when the office is reachable."""
    from app.api.report_pages import page_path
    from app.channels.notifications import deliver_pending, notify_run
    from app.db.models import AccessPoint

    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    _, run_id = await finished_run(client, monkeypatch, DASHBOARD)
    state = client.app_state  # type: ignore[attr-defined]
    settings = state.worker.settings
    with state.session_factory() as session, session.begin():
        row = session.get(RunRow, UUID(run_id))
        assert row is not None
        row.status = "COMPLETED"
        row.config_snapshot_json = {
            **(row.config_snapshot_json or {}),
            "origin": {
                "kind": "slack",
                "app_id": installation["app_id"],
                "channel": "D1",
                "thread": "1700.1",
            },
        }
        session.merge(
            AccessPoint(
                kind="remote",
                address="https://office.example.ts.net",
                enabled=True,
                verified_at=datetime.now(UTC),
                check_json={},
            )
        )
    notify_run(
        state.session_factory,
        UUID(run_id),
        "COMPLETED",
        store_of(state),
        lambda artifact_id: page_path(settings, str(artifact_id)),
    )
    await deliver_pending(state.session_factory, settings)
    thread = {channel: text for _, channel, text in fake_slack.sent}["D1"]
    assert "<https://office.example.ts.net/pages/" in thread and "|Read it as a page>" in thread
    names = [name for _, _, name, _ in fake_slack.uploads]
    assert names[0] == "chart-1.png" and names[-1].endswith(".docx")
    assert fake_slack.uploads[0][3][:4] == b"\x89PNG"


async def test_a_file_slack_refuses_is_recorded_and_the_message_not_resent(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    fake_slack.upload_fails_with = "missing_scope"
    state, run_id = await asked_in_slack(client, monkeypatch)
    notify_run(state.session_factory, run_id, "COMPLETED", store_of(state))
    await deliver_pending(state.session_factory, state.worker.settings)
    await deliver_pending(state.session_factory, state.worker.settings)
    assert [channel for _, channel, _ in fake_slack.sent].count("D1") == 1
    with state.session_factory() as session:
        origin = session.scalars(
            select(ChannelDeliveryRow).where(
                ChannelDeliveryRow.run_id == run_id, ChannelDeliveryRow.thread_ref == "1700.1"
            )
        ).one()
    assert (origin.status, origin.files_error) == ("SENT", "missing_scope")


async def test_the_owner_steers_a_working_task_from_its_thread(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels import slack_format
    from app.channels.inbound_office import SlackOffice

    state, run_id = await asked_in_slack(client, monkeypatch, status="RUNNING")
    [installation] = (await client.get("/channels")).json()["installations"]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(installation["app_id"]),
    )
    assert office.active_task("D1", "elsewhere") is None
    working = office.active_task("D1", "1700.1")
    assert working == {"run_id": str(run_id), "status": "RUNNING", "stuck": False}
    said = office.note_task(working, "Focus on Germany first")
    assert "note" in said
    [note] = [c for c in state.repository.controls(run_id) if c["kind"] == "instruction"]
    assert note["text"] == "Focus on Germany first"
    said = await office.act(slack_format.STOP, str(run_id))
    assert "Stopping" in said
    assert any(c["kind"] == "cancel" for c in state.repository.controls(run_id))
    said = await office.act(slack_format.REWORK, str(run_id))
    assert "still working" in said, "rework is for finished tasks"


async def test_a_schedule_posts_each_result_to_the_channel_it_names(
    client: httpx.AsyncClient, fake_slack: FakeSlack, no_dispatch: list[Any]
) -> None:
    """SL4: the run reports to that channel, as a message of its own."""
    from app.scheduler import tick

    channel = await slack_destination(client)
    schedule, _ = await new_schedule(client, frequency="weekly", weekday=0, post_to=channel)
    assert schedule["post_to"] == channel
    state = client.app_state  # type: ignore[attr-defined]
    due = datetime.fromisoformat(schedule["next_run_at"])
    [outcome] = tick(
        state.session_factory, state.repository, state.worker, state.worker.settings, due
    )
    with state.session_factory() as session:
        run = session.get(RunRow, UUID(outcome["run_id"]))
        assert run is not None
        origin = run.config_snapshot_json["origin"]
    assert (origin["channel"], origin["thread"]) == ("C1", None)
    assert origin["standing"] == "every Monday at 09:00"


async def test_a_standing_report_reaches_its_channel_once_with_its_file(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.notifications import deliver_pending, notify_run
    from app.channels.progress import sync_progress

    state, run_id = await asked_in_slack(client, monkeypatch)
    with state.session_factory() as session, session.begin():
        row = session.get(RunRow, run_id)
        assert row is not None
        snapshot = dict(row.config_snapshot_json)
        snapshot["origin"] = snapshot["origin"] | {
            "channel": "C1",
            "thread": None,
            "standing": "every Monday at 09:00",
        }
        row.config_snapshot_json = snapshot
    notify_run(state.session_factory, run_id, "COMPLETED", store_of(state))
    await deliver_pending(state.session_factory, state.worker.settings)
    [(_, channel, text)] = fake_slack.sent
    assert channel == "C1", "the channel subscribed to notices is not told twice"
    assert "Apply this quarter" in text and "A standing report, every Monday at 09:00" in text
    [(_, under, name, _)] = fake_slack.uploads
    assert under == "1.1" and name.endswith(".docx"), "the file goes under the report"
    with state.session_factory() as session, session.begin():
        row = session.get(RunRow, run_id)
        assert row is not None
        row.status = "RUNNING"
    assert await sync_progress(state.session_factory, state.worker.settings) == 0


async def test_a_slack_question_is_looked_up_and_answered_with_what_was_seen(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA1: the Buienradar case. A look in the office's browser, then the answer
    — now by the lead, which decides the look itself."""
    from app.channels.inbound_office import SlackOffice
    from app.channels.lookup import ChatAnswer
    from app.db.models import ChannelUsage

    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    state = client.app_state  # type: ignore[attr-defined]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(installation["app_id"]),
    )
    looked: list[str] = []

    async def look(address: str) -> tuple[str, bytes | None]:
        looked.append(address)
        return "Buienradar Lyon: droog", b"\xff\xd8radar"

    monkeypatch.setattr(
        "app.channels.inbound_office.office_models", lambda settings, _: LookupModel(settings)
    )
    monkeypatch.setattr(office, "_lookup_tools", lambda: (None, None, look))

    async def ok() -> str:
        return "All services ok."

    monkeypatch.setattr(office, "status_line", ok)
    LookupModel.seen = []
    LookupModel.replies = [
        ("look", {"address": "https://meteofrance.com/lyon"}),
        "Dry in Lyon until 16:00. _Looked at meteofrance.com, 14:52_",
    ]
    answer = await office.converse([], "Will it rain in Lyon at 15:00?")
    assert isinstance(answer, ChatAnswer)
    assert answer.text.startswith("Dry in Lyon") and answer.files == [
        ("looked-at-1.jpg", b"\xff\xd8radar")
    ]
    assert looked == ["https://meteofrance.com/lyon"]
    final = LookupModel.seen[1][-1]["content"]
    assert final[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"), "it saw the radar"
    with state.session_factory() as session:
        usage = session.scalars(select(ChannelUsage)).one()
    assert (usage.calls, usage.tokens) == (2, 240), "both calls count against the chat allowance"

    LookupModel.replies = [
        ("propose_team_run", {"objective": "Compare three CRMs", "why": "Needs a real comparison"}),
        "That needs a proper comparison; I proposed it as a task.",
    ]
    offered = await office.converse([], "Which CRM should we use?")
    assert isinstance(offered, ChatAnswer) and offered.task == "Compare three CRMs"


async def test_ideas_from_slack_are_kept_once_without_the_chat_allowance(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    """ID3: "idea: …" and a 💡 keep a thought under Ideas, once, with no model
    call; a message Slack will not show is said privately, not guessed."""
    from app.channels.inbound import InboundRequest
    from app.channels.inbound_office import SlackOffice
    from app.db.models import ChannelUsage

    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    state = client.app_state  # type: ignore[attr-defined]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(installation["app_id"]),
    )
    assert office.keep_idea("Sell via agencies", "slack:C1:1700.01") is True
    assert office.keep_idea("Sell via agencies", "slack:C1:1700.01") is False
    fake_slack.messages[("C1", "1700.02")] = "Rain alert before cycling"
    assert await office.message_text("C1", "1700.02") == "Rain alert before cycling"
    assert await office.message_text("C1", "1700.03") is None, "unreadable is None"
    await office.whisper("C1", "U0OWNER", "✓ Kept")
    assert fake_slack.whispers == [("C1", "U0OWNER", "✓ Kept")]
    new = (await client.get("/ideas")).json()["new"]
    assert [(i["text"], i["source"]) for i in new] == [("Sell via agencies", "slack")]
    with state.session_factory() as session:
        assert session.scalars(select(ChannelUsage)).first() is None, "no allowance used"
    assert InboundRequest  # the handler's own tests cover the words and the 💡


async def test_the_friday_reminder_goes_once_to_channels_that_asked(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    """ID4: Friday from 16:00 (owner's zone), the count only, once per channel."""
    from app.channels.notifications import notify_ideas_friday
    from app.db.models import ChannelDelivery, ChannelDestination

    state = client.app_state  # type: ignore[attr-defined]
    await slack_destination(client)
    with state.session_factory() as session, session.begin():
        destination = session.scalars(select(ChannelDestination)).first()
        destination.events_json = [*destination.events_json, "ideas_friday"]
    await client.post("/ideas", json={"text": "A secret plan"})
    thursday = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)
    friday_noon = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    friday_late = datetime(2026, 10, 16, 17, 0, tzinfo=UTC)
    assert notify_ideas_friday(state.session_factory, thursday) == 0
    assert notify_ideas_friday(state.session_factory, friday_noon) == 0
    assert notify_ideas_friday(state.session_factory, friday_late) == 1
    assert notify_ideas_friday(state.session_factory, friday_late) == 0, "once a Friday"
    with state.session_factory() as session:
        sent = session.scalars(
            select(ChannelDelivery).where(ChannelDelivery.event_kind == "ideas_friday")
        ).one()
    assert "1 idea not planned yet" in sent.text and "secret" not in sent.text


async def test_a_watch_started_from_slack_tells_the_thread_it_was_asked_in(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA5: the owner's Start on a watch offered in Slack starts it; what it is
    told goes back to that channel and thread, once; a bad address is refused."""
    monkeypatch.setattr("app.browser.safety.address_public", lambda host: host != "localhost")
    from types import SimpleNamespace

    from app import watches
    from app.channels.inbound_office import SlackOffice
    from app.db.models import ChannelDelivery, Watch

    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    state = client.app_state  # type: ignore[attr-defined]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(installation["app_id"]),
    )
    rain = {
        "kind": "page",
        "target": "https://meteofrance.com/lyon",
        "condition": "rain in Lyon before 15:00",
        "every_minutes": 60,
        "title": "Rain in Lyon",
    }
    said = office.start_watch(rain, "D9", "1700000000.1")
    assert "Rain in Lyon" in said and "Watching" in said
    listed = (await client.get("/watches")).json()["watches"]
    assert [w["title"] for w in listed] == ["Rain in Lyon"]
    assert listed[0]["post_to"]["name"] == "Chat thread"
    watch_id = UUID(listed[0]["id"])

    async def read(address: str) -> str:
        return "Lyon: rain from 14:20"

    async def complete(messages: list[dict[str, str]]) -> Any:
        return SimpleNamespace(
            content='{"tell": true, "message": "Rain from 14:20.", "done": true}',
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10),
            estimated_cost=0.001,
        )

    now = datetime.now(UTC)
    assert await watches.check(state.session_factory, watch_id, now, read, None, complete) == "told"
    with state.session_factory() as session:
        [sent] = session.scalars(
            select(ChannelDelivery).where(ChannelDelivery.event_kind == "watch")
        ).all()
        assert sent.thread_ref == "1700000000.1" and "Rain from 14:20." in sent.text
        assert session.get(Watch, watch_id).status == "ended"
    with pytest.raises(watches.WatchRejected):
        office.start_watch({**rain, "target": "http://localhost:8000/"}, "D9", None)


async def test_a_slack_lookup_opens_the_browser_only_when_browsing_is_on(
    client: httpx.AsyncClient,
    fake_slack: FakeSlack,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R19, 10 Oct: the Browsing setting means off for chat lookups too, as for tasks."""
    from app.channels.inbound_office import SlackOffice
    from app.channels.lookup import run_calls

    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    state = client.app_state  # type: ignore[attr-defined]
    settings = client.settings  # type: ignore[attr-defined]
    settings.browser_exchange = tmp_path / "browser-exchange"
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        settings,
        UUID(installation["app_id"]),
    )
    looked: list[str] = []

    async def look(exchange: Any, address: str) -> tuple[str, bytes | None]:
        looked.append(address)
        return "radar", None

    monkeypatch.setattr("app.lead.chat.look_with_browser", look)
    calls = [{"tool": "look", "input": "https://meteofrance.com/lyon"}]

    [off] = await run_calls(calls, *office._lookup_tools())
    assert looked == [] and off.error == "browser_switched_off"

    assert (await client.put("/settings", json={"browser_on": True})).status_code == 200
    [on] = await run_calls(calls, *office._lookup_tools())
    assert looked == ["https://meteofrance.com/lyon"] and on.text == "radar"


async def test_a_shared_file_without_permission_is_said_not_dropped(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA3: an app without files:read is told so, in the answer's own words."""
    from app.channels.base import ChannelError
    from app.channels.inbound_office import SlackOffice

    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    state = client.app_state  # type: ignore[attr-defined]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(installation["app_id"]),
    )

    async def refuse(token: str, url: str, limit: int) -> bytes:
        raise ChannelError("missing_scope")

    fake_slack.download = refuse  # type: ignore[attr-defined]
    images, notes = await office._shared(
        ({"name": "radar.png", "mimetype": "image/png", "size": 10, "url": "u"},)
    )
    assert images == [] and "needs the files:read permission" in notes[0]
    _, unread = await office._shared(({"name": "a.zip", "mimetype": "application/zip", "size": 9},))
    assert "cannot read here" in unread[0]


async def test_a_project_channel_takes_its_notifications_alone(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    everything = await slack_destination(client)  # the office's channel
    installation = (await client.get("/channels")).json()["installations"][0]
    theirs = (
        await client.post(
            f"/channels/installations/{installation['id']}/destinations",
            json={"external_id": "C2", "name": "#client-a", "events": ["completed"]},
        )
    ).json()
    state = client.app_state  # type: ignore[attr-defined]
    project_id, dispatch = await project_with_work(client, "Client with a channel")
    await client.put(
        f"/projects/{project_id}/settings", json={"notify_destination_id": theirs["id"]}
    )
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")
    await deliver_pending(state.session_factory, state.worker.settings)
    assert [channel for _, channel, _ in fake_slack.sent] == ["C2"]
    assert everything  # the office's own channel exists but heard nothing


async def test_an_unknown_delivery_is_reconciled_before_any_retry(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    """A send whose outcome is unknown is never repeated blindly."""
    from sqlalchemy import select

    from app.channels.notifications import deliver_pending, notify_run, reconcile_unknown
    from app.db.models import ChannelDelivery

    await slack_destination(client)
    state = client.app_state  # type: ignore[attr-defined]
    _, dispatch = await project_with_work(client, "Unknown outcome")
    fake_slack.fail_with = "outcome_unknown"
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []  # the send threw; nothing is known
    with state.session_factory() as session:
        delivery = session.scalars(
            select(ChannelDelivery).where(ChannelDelivery.run_id == dispatch.run_id)
        ).one()
        assert delivery.status == "UNKNOWN"
        text = delivery.text
    fake_slack.fail_with = None
    # Repeated delivery rounds must not send it again while it is unknown.
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []
    # The platform says it did arrive: the office records that, and sends nothing.
    fake_slack.history = [text]
    settled = await reconcile_unknown(state.session_factory, state.worker.settings)
    assert settled["delivered"] == 1
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []
    listing = (
        await client.get("/channels/deliveries", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert listing[0]["status"] == "SENT" and listing[0]["reconciled_by"] == "platform"


async def test_the_owner_can_settle_an_unknown_delivery(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    """Where the platform cannot be read, the owner decides — not a guess."""
    from app.channels.notifications import deliver_pending, notify_run

    await slack_destination(client)
    state = client.app_state  # type: ignore[attr-defined]
    _, dispatch = await project_with_work(client, "Owner decides")
    fake_slack.fail_with = "outcome_unknown"
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")
    await deliver_pending(state.session_factory, state.worker.settings)
    fake_slack.fail_with = None
    [delivery] = (
        await client.get("/channels/deliveries", params={"run_id": str(dispatch.run_id)})
    ).json()
    resolved = (
        await client.post(f"/channels/deliveries/{delivery['id']}/resolve", json={"arrived": True})
    ).json()
    assert resolved["status"] == "SENT"
    await deliver_pending(state.session_factory, state.worker.settings)
    assert fake_slack.sent == []
    again = await client.post(
        f"/channels/deliveries/{delivery['id']}/resolve", json={"arrived": False}
    )
    assert again.status_code == 409  # already settled


async def test_the_owner_adds_and_removes_who_may_use_the_bots(
    client: httpx.AsyncClient, fake_slack: Any
) -> None:
    """Membership changes in the office and nowhere else."""
    installation = (
        await client.post("/channels/slack/installations", json={"bot_token": BOT})
    ).json()
    listed = f"/channels/installations/{installation['id']}/members"

    added = await client.post(listed, json={"user_id": "U_COLLEAGUE"})
    assert added.status_code == 201
    assert added.json() == {"user_id": "U_COLLEAGUE", "role": "member"}
    who = (await client.get(listed)).json()
    assert {"user_id": "U_COLLEAGUE", "role": "member"} in [
        {"user_id": row["user_id"], "role": row["role"]} for row in who
    ]

    # Adding twice changes nothing.
    again = await client.post(listed, json={"user_id": "U_COLLEAGUE"})
    assert again.status_code == 409

    removed = await client.delete(f"{listed}/U_COLLEAGUE")
    assert removed.status_code == 204
    assert (await client.get(listed)).json() == []
    assert (await client.delete(f"{listed}/U_COLLEAGUE")).status_code == 404


async def test_the_owner_cannot_be_removed_as_if_they_were_a_member(
    client: httpx.AsyncClient, fake_slack: Any
) -> None:
    """Removing the owner would leave a paired workspace nobody can command."""
    from app.channels.members import set_owner

    installation = (
        await client.post("/channels/slack/installations", json={"bot_token": BOT})
    ).json()
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        set_owner(session, "slack", installation["workspace_id"], "U_OWNER")

    path = f"/channels/installations/{installation['id']}/members"
    assert (await client.delete(f"{path}/U_OWNER")).status_code == 404
    roles = {row["user_id"]: row["role"] for row in (await client.get(path)).json()}
    assert roles == {"U_OWNER": "owner"}


async def test_a_slack_proposal_survives_a_restart(client: httpx.AsyncClient) -> None:
    from tests.unit.test_inbound import Office, request

    state = client.app_state  # type: ignore[attr-defined]
    app_id = uuid4()
    before = Office(owner="U_OWNER")
    before.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    await _slack_handler(state, before, app_id).handle(
        request("Prepare this week's pricing brief", thread="t9")
    )
    assert before.started == []
    # The office restarts: a new handler and a new store over the same database.
    after = Office(owner="U_OWNER")
    await _slack_handler(state, after, app_id).handle(request("yes", event="e2", thread="t9"))
    assert after.started == [("p-1", "Prepare this week's pricing brief")]
    # Taken once: a second yes starts nothing more.
    again = Office(owner="U_OWNER")
    again.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    await _slack_handler(state, again, app_id).handle(request("yes", event="e3", thread="t9"))
    assert again.started == []


async def test_a_slack_thread_remembers_its_conversation_across_a_restart(
    client: httpx.AsyncClient,
) -> None:
    from tests.unit.test_inbound import Office, request

    state = client.app_state  # type: ignore[attr-defined]
    app_id = uuid4()
    before = Office(owner="U_OWNER")
    before.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    before.answer = "It found three vendors."
    await _slack_handler(state, before, app_id).handle(
        request("what did the last run find?", thread="t3")
    )
    after = Office(owner="U_OWNER")
    after.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    after.answer = "Before that, two."
    await _slack_handler(state, after, app_id).handle(
        request("and before that?", event="e2", thread="t3")
    )
    history, _ = after.conversations[-1]
    assert history[:2] == [
        {"role": "user", "content": "what did the last run find?"},
        {"role": "assistant", "content": "It found three vendors."},
    ]


async def test_a_limit_crossed_with_the_office_closed_reaches_slack_once(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending

    state = client.app_state  # type: ignore[attr-defined]
    settings = client.settings  # type: ignore[attr-defined]
    await slack_destination(client, ["limit_warning"])
    await client.put("/settings", json={"monthly_cost_limit_usd": 1.0})
    _, dispatch = await project_with_work(client, "Spend")
    _spend(state, dispatch, 0.85)
    # No browser reads the pulse: the scheduler's pass records and announces it.
    _crossings(state, settings)
    _crossings(state, settings)
    await deliver_pending(state.session_factory, state.worker.settings)
    assert len(fake_slack.sent) == 1, fake_slack.sent
    text = fake_slack.sent[0][2]
    assert "month" in text and "85%" in text
    await client.put("/settings", json={"monthly_cost_limit_usd": None})


_RESEARCH = {
    "research_question": "Which CRM suits five people?",
    "key_findings": ["CRM B is cheapest at five seats", "CRM A has the best mobile app"],
    "evidence": ["Price pages, Oct 2026"],
    "implications": ["Start with CRM B"],
    "unknowns": ["Support quality"],
    "sources": [
        {"title": "CRM B pricing", "url": "https://crmb.example/pricing", "source_type": "primary"},
        {"title": "Notes", "url": "local://notes/crm.md", "source_type": "secondary"},
    ],
    "confidence": "medium",
}


async def test_finished_research_sends_one_brief_to_its_thread_and_can_be_saved(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels import slack_format
    from app.channels.briefs import SAVED_SOURCE, queue_briefs
    from app.channels.inbound_office import SlackOffice
    from app.channels.notifications import deliver_pending
    from app.db.models import KnowledgeSource, SourceDocument

    state, run_id = await asked_in_slack(client, monkeypatch, status="RUNNING")
    assert queue_briefs(state.session_factory) == 0, "no research yet"
    state.repository.merge_state(run_id, {"research": _RESEARCH})
    state.repository.add_event(run_id, "step_completed", {"node": "research"})
    assert queue_briefs(state.session_factory) == 1
    assert queue_briefs(state.session_factory) == 0, "once a task"
    await deliver_pending(state.session_factory, state.worker.settings)
    [(_, channel, text)] = fake_slack.sent
    assert channel == "D1" and fake_slack.threads == ["1700.1"], "the thread only"
    assert "Research brief" in text and "CRM B is cheapest" in text
    assert "<https://crmb.example/pricing|CRM B pricing>" in text
    assert "local://" not in text, "an owner's own document is not linked"
    assert "Sources: 2 (1 primary)" in text and "Still unknown: 1" in text
    [save] = fake_slack.blocks[0][-1]["elements"]
    assert save["action_id"] == slack_format.SAVE and save["value"] == str(run_id)

    [installation] = (await client.get("/channels")).json()["installations"]
    office = SlackOffice(
        state.session_factory,
        state.repository,
        state.worker,
        state.approvals,
        state.worker.settings,
        UUID(installation["app_id"]),
    )
    assert await office.act(slack_format.SAVE, str(run_id)) == (
        f"✓ Saved to Knowledge, under {SAVED_SOURCE}."
    )
    await office.act(slack_format.SAVE, str(run_id))
    with state.session_factory() as session:
        source = session.scalars(
            select(KnowledgeSource).where(KnowledgeSource.name == SAVED_SOURCE)
        ).one()
        [document] = session.scalars(
            select(SourceDocument).where(SourceDocument.source_id == source.id)
        ).all()
    assert source.kind == "upload" and source.document_count == 1, "saved again, not twice"
    assert "## Findings" in document.text and "CRM B is cheapest" in document.text
    assert document.path.endswith(f"({str(run_id)[:8]}).md")


async def test_the_daily_digest_goes_once_from_six_with_counts_only(
    client: httpx.AsyncClient, fake_slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import ideas
    from app.channels.digest import notify_daily_digest
    from app.db.models import ChannelDelivery, ChannelDestination

    state, run_id = await asked_in_slack(client, monkeypatch)
    with state.session_factory() as session, session.begin():
        zone = ideas.owner_zone(session)
        destination = session.scalars(
            select(ChannelDestination).where(ChannelDestination.external_id == "C1")
        ).one()
        destination.events_json = [*destination.events_json, "daily_digest"]
        run = session.get(RunRow, run_id)
        assert run is not None
        today = datetime.now(UTC).astimezone(zone).date()
        run.completed_at = datetime(today.year, today.month, today.day, 12, tzinfo=zone)
    afternoon = datetime(today.year, today.month, today.day, 17, 30, tzinfo=zone)
    evening = datetime(today.year, today.month, today.day, 18, 5, tzinfo=zone)
    assert notify_daily_digest(state.session_factory, afternoon) == 0, "not before six"
    assert notify_daily_digest(state.session_factory, evening) == 1
    assert notify_daily_digest(state.session_factory, evening) == 0, "once a day"
    with state.session_factory() as session:
        sent = session.scalars(
            select(ChannelDelivery).where(ChannelDelivery.event_kind == "daily_digest")
        ).one()
    assert "Daily digest" in sent.text and "Finished: 1" in sent.text
    assert "EMI" not in sent.text, "what the work says stays in the office"
    assert sent.blocks_json and sent.blocks_json[0]["type"] == "context"
