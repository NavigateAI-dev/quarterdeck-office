"""The team: agents, skills, members and rules."""

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from tests.integration.api_support import (
    AGENT_APP,
    AGENT_BOT,
    BOT,
    NEWER_COMMIT,
    PROVIDER_KEY,
    FakeAdapter,
    FakeDirect,
    FakeSlack,
    _changed,
    _found,
    _granted_filings,
    _installed,
    _room_with_team,
    _said,
    _waiting,
    agent_with_bot,
    checked_connection,
    new_agent,
    new_connection,
    slack_destination,
)

pytestmark = pytest.mark.integration


async def test_editing_creates_a_version_without_enabling_it(client: httpx.AsyncClient) -> None:
    agent = await new_agent(client)
    edited = await client.post(f"/agents/{agent['id']}/versions", json={"instructions": "v2"})
    assert edited.status_code == 201
    body = edited.json()
    assert body["enabled_version"] == 1
    assert [v["instructions"] for v in body["versions"]] == ["Cite sources.", "v2"]
    enabled = await client.post(f"/agents/{agent['id']}/versions/2/enable")
    assert enabled.json()["enabled_version"] == 2


async def test_enabling_an_unknown_version_is_not_found(client: httpx.AsyncClient) -> None:
    agent = await new_agent(client)
    response = await client.post(f"/agents/{agent['id']}/versions/9/enable")
    assert response.status_code == 404


async def test_only_a_tested_connection_can_be_assigned(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    untested = await new_connection(client, display_name="Untested")
    refused = await client.post(
        "/agents",
        json={"name": "A", "role": "researcher", "connection_id": untested["id"]},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "connection_not_tested"
    connection_id = await checked_connection(client, monkeypatch)
    agent = await new_agent(client, connection_id=connection_id)
    assert agent["versions"][0]["connection_id"] == connection_id


async def test_assigning_a_missing_connection_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/agents", json={"name": "A", "role": "researcher", "connection_id": str(uuid4())}
    )
    assert response.status_code == 404


async def test_agent_names_are_unique(client: httpx.AsyncClient) -> None:
    await new_agent(client)
    duplicate = await client.post("/agents", json={"name": "Researcher", "role": "writer"})
    assert duplicate.status_code == 409


async def test_agents_require_the_owner(client: httpx.AsyncClient) -> None:
    response = await client.get("/agents", headers={"Authorization": ""})
    assert response.status_code == 401


async def test_a_run_keeps_the_configuration_it_started_with(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = await checked_connection(client, monkeypatch)
    agent = await new_agent(client, connection_id=connection_id)
    project = (await client.post("/projects", json={"name": "Snapshot"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Assess"})
    ).json()

    await client.post(f"/agents/{agent['id']}/versions", json={"instructions": "changed"})
    await client.post(f"/agents/{agent['id']}/versions/2/enable")

    snapshot = (await client.get(f"/runs/{run['id']}")).json()["config_snapshot"]
    [captured] = snapshot["agents"]
    assert captured["profile_id"] == agent["id"]
    assert captured["version"] == 1
    assert captured["instructions"] == "Cite sources."
    assert captured["connection_id"] == connection_id
    assert captured["provider"] == "openai"
    assert captured["model_id"] == "gpt-5"
    assert PROVIDER_KEY not in str(snapshot)


async def test_an_agent_test_run_uses_its_assigned_model_and_reports_usage(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeDirect.made, FakeDirect.fail_with = [], None
    monkeypatch.setattr("app.api.routers.team.DirectCompleter", FakeDirect)
    connection_id = await checked_connection(client, monkeypatch)
    agent = await new_agent(client, connection_id=connection_id)
    body = (await client.post(f"/agents/{agent['id']}/test", json={"billable": True})).json()
    assert body["model"] == "gpt-5-2026"
    assert body["version"] == 1
    assert body["usage"] == {"prompt_tokens": 40, "completion_tokens": 8}
    assert body["reply"] == "I research and cite sources."
    assert FakeDirect.made == [{"provider": "openai", "model": "gpt-5", "credential": PROVIDER_KEY}]
    assert PROVIDER_KEY not in json.dumps(body)


async def test_an_agent_test_run_needs_billing_consent(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = await checked_connection(client, monkeypatch)
    agent = await new_agent(client, connection_id=connection_id)
    response = await client.post(f"/agents/{agent['id']}/test", json={"billable": False})
    assert response.status_code == 409
    assert response.json()["detail"] == "billable_confirmation_required"


async def test_an_agent_without_a_model_cannot_be_tested(client: httpx.AsyncClient) -> None:
    agent = await new_agent(client)
    response = await client.post(f"/agents/{agent['id']}/test", json={"billable": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "agent_has_no_model"


async def test_a_provider_failure_in_a_test_run_is_reported_as_its_code(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeDirect.made, FakeDirect.fail_with = [], "rate_limited"
    monkeypatch.setattr("app.api.routers.team.DirectCompleter", FakeDirect)
    connection_id = await checked_connection(client, monkeypatch)
    agent = await new_agent(client, connection_id=connection_id)
    response = await client.post(f"/agents/{agent['id']}/test", json={"billable": True})
    assert response.status_code == 502
    assert response.json()["detail"] == "rate_limited"


async def test_the_catalog_lists_curated_skills_with_permissions(client: httpx.AsyncClient) -> None:
    catalog = (await client.get("/skills/catalog")).json()
    brief = next(s for s in catalog if s["skill_id"] == "competitor-brief")
    assert [v["version"] for v in brief["versions"]] == ["1.0.0", "1.1.0"]
    assert brief["versions"][1]["permissions"] == ["web_search", "local_documents"]
    assert brief["versions"][0]["review"]["status"] == "reviewed"


async def test_installing_a_skill_enables_it(client: httpx.AsyncClient) -> None:
    body = (
        await client.post(
            "/skills/install", json={"skill_id": "comparison-table", "version": "1.0.0"}
        )
    ).json()
    assert body["status"] == "ENABLED"
    assert body["permissions"] == []
    assert [s["skill_id"] for s in (await client.get("/skills")).json()] == ["comparison-table"]


async def test_an_update_that_adds_permissions_needs_review(client: httpx.AsyncClient) -> None:
    """A12: a permission-expanding update is never enabled silently."""
    await client.post("/skills/install", json={"skill_id": "competitor-brief", "version": "1.0.0"})
    update = (
        await client.post(
            "/skills/install", json={"skill_id": "competitor-brief", "version": "1.1.0"}
        )
    ).json()
    assert update["status"] == "NEEDS_REVIEW"
    assert update["new_permissions"] == ["local_documents"]
    approved = await client.post("/skills/competitor-brief/versions/1.1.0/approve")
    assert approved.json()["status"] == "ENABLED"


async def test_a_skill_awaiting_review_cannot_be_pinned(client: httpx.AsyncClient) -> None:
    await client.post("/skills/install", json={"skill_id": "competitor-brief", "version": "1.0.0"})
    await client.post("/skills/install", json={"skill_id": "competitor-brief", "version": "1.1.0"})
    response = await client.post(
        "/agents",
        json={
            "name": "Analyst",
            "role": "researcher",
            "skills": [{"skill_id": "competitor-brief", "version": "1.1.0"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "skill_not_enabled:competitor-brief@1.1.0"


async def test_uninstalling_reports_affected_agents_and_keeps_history(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/skills/install", json={"skill_id": "comparison-table", "version": "1.0.0"})
    await new_agent(
        client,
        name="Writer",
        role="summarist",
        skills=[{"skill_id": "comparison-table", "version": "1.0.0"}],
    )
    body = (await client.delete("/skills/comparison-table/versions/1.0.0")).json()
    assert body["status"] == "UNINSTALLED"
    assert body["affected_agents"] == ["Writer"]
    history = (await client.get("/skills", params={"include_uninstalled": True})).json()
    assert history[0]["status"] == "UNINSTALLED"


async def test_installing_an_unknown_skill_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/skills/install", json={"skill_id": "rm-rf", "version": "1.0.0"})
    assert response.status_code == 404


async def test_an_agent_gets_its_own_bot_from_a_generated_manifest(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    agent = await new_agent(client, name="Pricing researcher")
    before = (await client.get(f"/agents/{agent['id']}/chat-bot/slack")).json()
    assert before["installation"] is None
    assert before["manifest"]["display_information"]["name"] == "Pricing researcher"
    assert before["create_app_url"].startswith("https://api.slack.com/apps?new_app=1")
    response = await client.put(
        f"/agents/{agent['id']}/chat-bot/slack",
        json={"bot_token": AGENT_BOT, "app_token": AGENT_APP},
    )
    assert response.status_code == 200, response.text
    assert AGENT_BOT not in response.text and AGENT_APP not in response.text
    bot = response.json()
    assert bot["installation"]["bot_user_id"] == "U0RESEARCHER"
    assert bot["app_token_configured"] is True
    # The office's Messaging list shows the office bot only.
    assert (await client.get("/channels")).json()["installations"] == []


async def test_one_slack_bot_cannot_speak_for_two_agents(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    await client.post("/channels/slack/installations", json={"bot_token": BOT})
    agent = await new_agent(client)
    response = await client.put(f"/agents/{agent['id']}/chat-bot/slack", json={"bot_token": BOT})
    assert response.status_code == 409
    assert response.json()["detail"] == "bot_already_in_use"


async def test_disconnecting_an_agent_bot_deletes_its_tokens(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.inbound_office import listening_apps

    agent = await agent_with_bot(client)
    state = client.app_state  # type: ignore[attr-defined]
    assert len(listening_apps(state.session_factory)) == 1
    response = await client.delete(f"/agents/{agent['id']}/chat-bot/slack")
    assert response.status_code == 204
    bot = (await client.get(f"/agents/{agent['id']}/chat-bot/slack")).json()
    assert bot["installation"] is None and bot["app_token_configured"] is False
    assert listening_apps(state.session_factory) == set()


async def test_an_archived_agents_bot_stops_listening(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.inbound_office import listening_apps

    agent = await agent_with_bot(client)
    state = client.app_state  # type: ignore[attr-defined]
    await client.post(f"/agents/{agent['id']}/archive")
    assert listening_apps(state.session_factory) == set()


async def test_work_asked_of_an_agent_is_reported_by_that_agent_in_the_thread(
    client: httpx.AsyncClient, fake_slack: FakeSlack
) -> None:
    from app.channels.notifications import deliver_pending, notify_run

    await slack_destination(client)  # the office bot's channel still hears everything
    agent = await agent_with_bot(client)
    other = await new_agent(client, name="Critic", role="critic")
    await client.put(
        f"/agents/{other['id']}/chat-bot/slack", json={"bot_token": "xoxb-agent-token-critic"}
    )
    other_bot = (await client.get(f"/agents/{other['id']}/chat-bot/slack")).json()
    await client.post(
        f"/channels/installations/{other_bot['installation']['id']}/destinations",
        json={"external_id": "C9", "name": "#critic", "events": ["completed"]},
    )
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Threads"})).json()
    dispatch = state.repository.create(
        UUID(project["id"]),
        "Find list prices",
        workflow="agent_research",
        origin={
            "kind": "slack",
            "app_id": agent["bot"]["installation"]["app_id"],
            "agent_profile_id": agent["id"],
            "channel": "D1",
            "thread": "1700.1",
        },
    )
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")
    notify_run(state.session_factory, dispatch.run_id, "COMPLETED")  # replayed
    await deliver_pending(state.session_factory, state.worker.settings)
    sent = sorted(zip(fake_slack.sent, fake_slack.threads, strict=True))
    assert [(token, channel, thread) for (token, channel, _), thread in sent] == [
        (AGENT_BOT, "D1", "1700.1"),  # the agent, in the thread it was asked in
        (BOT, "C1", None),  # the office bot, as before; the critic's bot stays silent
    ]
    records = (
        await client.get("/channels/deliveries", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert {r["agent_profile_id"] for r in records} == {None, agent["id"]}


async def test_policies_are_configured_per_office_and_per_project(
    client: httpx.AsyncClient,
) -> None:
    available = (await client.get("/policies")).json()["available"]
    assert {kind["id"] for kind in available} >= {"no_web", "web_allow_list", "risk_score"}
    project = (await client.post("/projects", json={"name": "Private client"})).json()
    other = (await client.post("/projects", json={"name": "Open work"})).json()
    created = await client.post(
        "/policies", json={"kind": "no_web", "scope": "project", "project_id": project["id"]}
    )
    assert created.status_code == 201
    office = await client.post("/policies", json={"kind": "max_tool_calls", "params": {"limit": 5}})
    assert office.status_code == 201
    listing = (await client.get("/policies")).json()["policies"]
    assert {row["kind"]: row["scope"] for row in listing} == {
        "no_web": "project",
        "max_tool_calls": "office",
    }
    unknown = await client.post("/policies", json={"kind": "nonsense"})
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown_policy"
    needs_project = await client.post("/policies", json={"kind": "no_web", "scope": "project"})
    assert needs_project.status_code == 422
    # The project's own rule applies to it, and not to another project.
    from app.policies.engine import Hook
    from app.policies.store import PolicyGate

    state = client.app_state  # type: ignore[attr-defined]
    gate = PolicyGate(state.session_factory)
    refused = gate.check(
        Hook.TOOL_CALL, "web_fetch", project_id=UUID(project["id"]), url="https://example.com"
    )
    assert refused.verdict.value == "deny" and refused.policy == "no_web"
    allowed = gate.check(
        Hook.TOOL_CALL, "web_fetch", project_id=UUID(other["id"]), url="https://example.com"
    )
    assert allowed.verdict.value == "allow"
    # Turning it off restores what the office did before.
    policy_id = next(row["id"] for row in listing if row["kind"] == "no_web")
    await client.put(f"/policies/{policy_id}", json={"enabled": False})
    assert (
        gate.check(
            Hook.TOOL_CALL, "web_fetch", project_id=UUID(project["id"]), url="https://example.com"
        ).verdict.value
        == "allow"
    )
    assert (await client.delete(f"/policies/{policy_id}")).status_code == 204


async def test_a_refused_tool_call_stops_the_step_and_is_recorded(
    client: httpx.AsyncClient,
) -> None:
    from app.policies.engine import Hook
    from app.policies.guard import GuardedFetcher
    from app.policies.store import PolicyGate, PolicyRefused

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "No web here"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "read something")
    await client.post(
        "/policies", json={"kind": "no_web", "scope": "project", "project_id": project["id"]}
    )

    class Fetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch(self, url: str, **kwargs: Any) -> str:
            self.calls.append(url)
            return "page"

    inner = Fetcher()
    gate = PolicyGate(state.session_factory)
    guarded = GuardedFetcher(inner, gate, dispatch.run_id, UUID(project["id"]))
    with pytest.raises(PolicyRefused) as refusal:
        await guarded.fetch("https://example.com/pricing")
    assert "own documents" in str(refusal.value)
    assert inner.calls == [], "the page was fetched despite the refusal"
    records = (
        await client.get("/policies/records", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert records[0]["verdict"] == "deny" and records[0]["policy"] == "no_web"
    assert records[0]["detail"]["url"] == "https://example.com/pricing"
    # A policy that counts keeps its counters between steps.
    await client.post("/policies", json={"kind": "risk_score", "params": {"ask_at": 2}})
    second = gate.check(
        Hook.TOOL_CALL,
        "local_documents",
        run_id=dispatch.run_id,
        project_id=UUID(project["id"]),
    )
    assert second.verdict.value == "allow"
    again = gate.check(
        Hook.TOOL_CALL,
        "engineering",
        run_id=dispatch.run_id,
        project_id=UUID(project["id"]),
    )
    assert again.verdict.value == "ask"


async def test_policies_need_the_owner(client: httpx.AsyncClient) -> None:
    for path in ("/policies", "/policies/records"):
        assert (await client.get(path, headers={"Authorization": ""})).status_code == 401


async def test_a_rule_can_pause_a_task_until_the_owner_answers(
    client: httpx.AsyncClient,
) -> None:
    """An "ask" stops the step, records the question, and waits."""
    from app.policies.guard import GuardedFetcher
    from app.policies.store import PolicyAsk, PolicyGate

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Ask first"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "read a page")
    await client.post(
        "/policies",
        json={"kind": "ask_before_web", "scope": "project", "project_id": project["id"]},
    )

    class Fetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch(self, url: str, **kwargs: Any) -> str:
            self.calls.append(url)
            return "page"

    inner = Fetcher()
    gate = PolicyGate(state.session_factory)
    guarded = GuardedFetcher(inner, gate, dispatch.run_id, UUID(project["id"]))
    with pytest.raises(PolicyAsk) as asked:
        await guarded.fetch("https://example.com/pricing")
    assert inner.calls == []
    record_id = asked.value.record_id
    assert record_id is not None
    records = (
        await client.get("/policies/records", params={"run_id": str(dispatch.run_id)})
    ).json()
    assert records[0]["verdict"] == "ask" and records[0]["decided_at"] is None
    # Answering it grants permission for this task only, and only once.
    answered = (
        await client.post(f"/policies/records/{record_id}/decide", json={"allow": True})
    ).json()
    assert answered["decision"] == "allow"
    again = await client.post(f"/policies/records/{record_id}/decide", json={"allow": True})
    assert again.status_code == 409 and again.json()["detail"] == "already_decided"
    # The same task may now read the web; another task still has to ask.
    await guarded.fetch("https://example.com/pricing")
    assert inner.calls == ["https://example.com/pricing"]
    second = state.repository.create(UUID(project["id"]), "another task")
    elsewhere = GuardedFetcher(inner, gate, second.run_id, UUID(project["id"]))
    with pytest.raises(PolicyAsk):
        await elsewhere.fetch("https://example.com/pricing")


async def test_refusing_a_paused_task_leaves_it_paused_with_the_reason(
    client: httpx.AsyncClient,
) -> None:
    from app.policies.guard import GuardedFetcher
    from app.policies.store import PolicyAsk, PolicyGate

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Refused"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "read a page")
    await client.post(
        "/policies",
        json={"kind": "ask_before_web", "scope": "project", "project_id": project["id"]},
    )

    class Fetcher:
        async def fetch(self, url: str, **kwargs: Any) -> str:
            raise AssertionError("fetched despite a refusal")

    gate = PolicyGate(state.session_factory)
    guarded = GuardedFetcher(Fetcher(), gate, dispatch.run_id, UUID(project["id"]))
    with pytest.raises(PolicyAsk) as asked:
        await guarded.fetch("https://example.com")
    refused = (
        await client.post(
            f"/policies/records/{asked.value.record_id}/decide", json={"allow": False}
        )
    ).json()
    assert refused == {"decision": "refuse", "resumed": False}
    with pytest.raises(PolicyAsk):
        await guarded.fetch("https://example.com")  # still not permitted


async def test_an_agent_exports_to_a_file_and_comes_back(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent is portable: readable, keepable, restorable."""
    import yaml

    monkeypatch.setattr(
        "app.api.routers.reach.adapter_for", lambda p, base_url=None: FakeAdapter(["m"])
    )
    connection = await checked_connection(client, monkeypatch, name="Portable model")
    agent = await new_agent(
        client,
        name="Pricing researcher",
        role="researcher",
        instructions="Quote list prices in EUR.",
        connection_id=connection,
    )
    exported = await client.get(f"/agents/{agent['id']}/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    document = yaml.safe_load(exported.text)
    assert document["name"] == "Pricing researcher" and document["role"] == "researcher"
    assert document["instructions"] == "Quote list prices in EUR."
    assert document["model"] == {"provider": "openai", "model": "gpt-5"}
    assert "id" not in document and "connection_id" not in document  # nothing of this office
    assert PROVIDER_KEY not in exported.text
    # Reading it back says what it would do, and changes nothing yet.
    proposed = (await client.post("/agents/import", json={"document": exported.text})).json()
    assert proposed["imported"] is False
    assert proposed["plan"]["name_taken"] is True and proposed["plan"]["can_import"] is False
    assert "already exists" in proposed["plan"]["notes"][0]
    refused = await client.post("/agents/import", json={"document": exported.text, "confirm": True})
    assert refused.status_code == 409
    # Under another name it imports, keeping the model it named.
    renamed = exported.text.replace("Pricing researcher", "Pricing researcher (copy)")
    done = (await client.post("/agents/import", json={"document": renamed, "confirm": True})).json()
    assert done["imported"] is True
    restored = done["agent"]
    assert restored["name"] == "Pricing researcher (copy)"
    version = restored["versions"][0]
    assert version["instructions"] == "Quote list prices in EUR."
    assert str(version["connection_id"]) == connection


async def test_an_import_cannot_grant_a_skill_the_office_has_not_approved(
    client: httpx.AsyncClient,
) -> None:
    import yaml

    document = yaml.safe_dump(
        {
            "format": 1,
            "name": "Imported agent",
            "role": "researcher",
            "instructions": "Be careful.",
            "model": {"provider": "openai", "model": "nothing-here"},
            "skills": [{"skill_id": "source-check", "version": "9.9.9"}],
            "limits": {},
        }
    )
    plan = (await client.post("/agents/import", json={"document": document})).json()["plan"]
    assert plan["skills"] == []
    assert any("not installed and enabled" in note for note in plan["notes"])
    assert any("No model connection" in note for note in plan["notes"])
    done = (
        await client.post("/agents/import", json={"document": document, "confirm": True})
    ).json()
    assert done["imported"] is True
    assert done["agent"]["versions"][0]["skills"] == []
    assert done["agent"]["versions"][0]["connection_id"] is None


async def test_a_file_that_is_not_an_agent_is_refused(client: httpx.AsyncClient) -> None:
    for document, detail in (
        ("just a string", "not_an_agent"),
        ("format: 99\nname: x\nrole: y\n", "unsupported_format"),
        ("format: 1\nname: ''\nrole: researcher\n", "name_and_role_required"),
        ("format: 1\nname: [unclosed\n", "not_yaml"),
    ):
        response = await client.post("/agents/import", json={"document": document})
        assert response.status_code == 422, document
        assert response.json()["detail"] == detail


async def test_a_tool_needing_approval_pauses_the_task_until_the_owner_allows_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N5b: asked once, answered where rules are answered, then called."""
    from app.mcp.client import ToolResult
    from app.policies.store import PolicyAsk, PolicyGate

    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "resume_paused", lambda run_id: True)
    _, project = await _granted_filings(client, monkeypatch)
    dispatch = state.repository.create(UUID(project["id"]), "file it")
    tools = await state.worker._mcp_tools(dispatch, PolicyGate(state.session_factory))
    assert tools is not None

    sent: list[str] = []

    async def call(
        self: Any, url: str, token: Any, allow: Any, name: str, arguments: dict
    ) -> ToolResult:
        sent.append(name)
        return ToolResult(content="opened", failed=False)

    monkeypatch.setattr("app.mcp.client.McpClient.call", call)
    server_name = tools.servers[0].name
    with pytest.raises(PolicyAsk) as asked:
        await tools.call(server_name, "file_issue", {"title": "Wwft gap"})
    # Asked again before an answer: the same question, not a second one.
    with pytest.raises(PolicyAsk) as again:
        await tools.call(server_name, "file_issue", {"title": "Wwft gap"})
    assert again.value.record_id == asked.value.record_id
    assert sent == []

    records = (
        await client.get("/policies/records", params={"run_id": str(dispatch.run_id)})
    ).json()
    open_questions = [row for row in records if row["verdict"] == "ask" and not row["decided_at"]]
    assert len(open_questions) == 1
    assert "Wwft gap" in open_questions[0]["reason"]

    decided = await client.post(
        f"/policies/records/{asked.value.record_id}/decide", json={"allow": True}
    )
    assert decided.json()["decision"] == "allow"
    assert await tools.call(server_name, "file_issue", {"title": "Wwft gap"}) == "opened"
    assert sent == ["file_issue"]


async def test_an_unknown_or_archived_name_is_answered_by_the_office_for_nothing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, fake = await _room_with_team(client, monkeypatch)
    otto = await new_agent(client, name="Otto", role="strategist")
    await client.post(f"/agents/{otto['id']}/archive")
    await client.post(f"/projects/{project_id}/room", json={"text": "@Bob @Otto hello?"})
    assert fake.asked == []
    said = await _said(client, project_id)
    office = [m for m in said if m["author"]["kind"] == "office"]
    assert len(office) == 1
    assert "Bob" in office[0]["text"] and "Otto" in office[0]["text"]
    assert office[0]["cost_usd"] in (None, 0)


async def test_a_rule_or_tool_question_waits_in_the_room_until_it_is_decided(
    client: httpx.AsyncClient,
) -> None:
    from app.policies.store import PolicyAsk, PolicyGate

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Rule {uuid4().hex[:6]}"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "File the gap")
    gate = PolicyGate(state.session_factory)
    with pytest.raises(PolicyAsk) as asked:
        gate.ask_owner(
            "mcp:Filings:file_issue",
            "This task wants to call file_issue on Filings.",
            "mcp_call",
            run_id=dispatch.run_id,
            project_id=UUID(project["id"]),
        )
    [question] = await _waiting(client, project["id"])
    assert question["kind"] == "policy"
    assert question["record_id"] == asked.value.record_id
    assert "file_issue" in question["reason"]
    monkeypatch_resume = state.worker.resume_paused
    state.worker.resume_paused = lambda run_id: True
    try:
        await client.post(f"/policies/records/{asked.value.record_id}/decide", json={"allow": True})
    finally:
        state.worker.resume_paused = monkeypatch_resume
    assert await _waiting(client, project["id"]) == []


async def test_a_skill_is_previewed_before_anything_is_installed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _found()
    asked: list[tuple] = []

    async def fetch(owner: str, repo: str, skill: Any, allow: Any, commit: Any = None) -> Any:
        asked.append((owner, repo, skill, commit))
        return found

    monkeypatch.setattr("app.skills.ecosystem.fetch", fetch)
    preview = await client.post(
        "/skills/ecosystem/preview",
        json={"source": "https://www.skills.sh/acme/skills/to-questionnaire"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["commit"] == "c" * 40 and body["refused"] == ["scripts/fill.py"]
    assert body["audits"]["snyk"] == "Warn" and body["assumptions"][0]["kind"] == "files"
    assert asked == [("acme", "skills", "to-questionnaire", None)]
    installed = [s["skill_id"] for s in (await client.get("/skills")).json()]
    assert not any(s.startswith("to-questionnaire") for s in installed), "nothing installed yet"


async def test_installing_what_was_previewed_pins_it_to_its_commit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _found()
    commits: list[Any] = []

    async def fetch(owner: str, repo: str, skill: Any, allow: Any, commit: Any = None) -> Any:
        commits.append(commit)
        return found

    monkeypatch.setattr("app.skills.ecosystem.fetch", fetch)
    body = {"source": "acme/skills to-questionnaire", "commit": "c" * 40, "role": "summarist"}
    installed = await client.post("/skills/ecosystem/install", json=body)
    assert installed.status_code == 201, installed.text
    view = installed.json()
    assert view["status"] == "ENABLED" and view["roles"] == ["summarist"]
    assert view["source"]["commit"] == "c" * 40 and view["source"]["repo"] == "acme/skills"
    assert commits == ["c" * 40], "it reads the commit the owner previewed, not whatever is newest"
    again = await client.post("/skills/ecosystem/install", json=body)
    assert again.status_code == 201 and again.json()["version"] == view["version"]
    listed = [s for s in (await client.get("/skills")).json() if s["skill_id"] == view["skill_id"]]
    assert len(listed) == 1


async def test_a_repository_with_several_skills_is_asked_which(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.skills.ecosystem import EcosystemError

    async def fetch(*args: Any, **kwargs: Any) -> Any:
        raise EcosystemError("choose_skill", ["seo-audit", "copywriting"])

    monkeypatch.setattr("app.skills.ecosystem.fetch", fetch)
    asked = await client.post("/skills/ecosystem/preview", json={"source": "acme/marketing"})
    assert asked.status_code == 409
    assert asked.json()["detail"] == {
        "code": "choose_skill",
        "choices": ["seo-audit", "copywriting"],
    }
    bad = await client.post("/skills/ecosystem/preview", json={"source": "not a source"})
    assert bad.status_code == 422


async def test_a_run_is_given_the_enabled_skills_its_project_can_use(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    found = _found()
    needs_web = _found(name=f"web-{uuid4().hex[:6]}")
    needs_web.permissions = ["local_documents"]

    async def fetch(owner: str, repo: str, skill: Any, allow: Any, commit: Any = None) -> Any:
        return found if skill == "q" else needs_web

    monkeypatch.setattr("app.skills.ecosystem.fetch", fetch)
    for skill in ("q", "w"):
        await client.post(
            "/skills/ecosystem/install",
            json={"source": f"acme/skills {skill}", "commit": "c" * 40, "role": "summarist"},
        )
    project = (await client.post("/projects", json={"name": f"Skills {uuid4().hex[:6]}"})).json()
    library = state.worker._skill_library(UUID(project["id"]))
    ours = {
        key: value for key, value in library.items() if key.startswith(("to-questionnaire", "web-"))
    }
    assert list(ours) == [found.name], "a skill needing a permission the project lacks is left out"
    entry = ours[found.name]
    assert entry["roles"] == ["summarist"] and entry["source"] == "acme/skills"
    assert entry["instructions"].startswith("Ask who it goes to.")


async def test_an_improved_skill_says_an_update_is_available(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _found()
    view = await _installed(client, monkeypatch, {"c" * 40: found})
    _changed(monkeypatch)
    checked = await client.post("/skills/updates/check")
    assert checked.status_code == 200, checked.text
    mine = next(u for u in checked.json() if u["skill_id"] == view["skill_id"])
    assert mine["state"] == "available"
    assert mine["latest_commit"] == NEWER_COMMIT and mine["message"] == "Tighten the questions"
    listed = (await client.get("/skills/updates")).json()
    assert any(u["skill_id"] == view["skill_id"] and u["state"] == "available" for u in listed)

    # GitHub unreachable: the last answer stands, and it says it could not check.
    _changed(monkeypatch, fails="github_rate_limited")
    again = next(
        u
        for u in (await client.post("/skills/updates/check")).json()
        if u["skill_id"] == view["skill_id"]
    )
    assert again["state"] == "unreachable" and again["latest_commit"] == NEWER_COMMIT


async def test_an_update_is_previewed_as_what_changed_and_reviewed_if_it_asks_for_more(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _found()
    found.instructions = "Ask who it goes to.\nKeep it short."
    newer = _found(commit=NEWER_COMMIT, name=found.name)
    newer.instructions = "Ask who it goes to.\nSay why each question matters."
    newer.permissions = ["web_search"]
    view = await _installed(client, monkeypatch, {"c" * 40: found, NEWER_COMMIT: newer})
    _changed(monkeypatch)
    await client.post("/skills/updates/check")

    preview = await client.post(f"/skills/{view['skill_id']}/update/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert {"op": "+", "text": "Say why each question matters."} in body["changes"]
    assert {"op": "-", "text": "Keep it short."} in body["changes"]
    assert body["permissions_added"] == ["web_search"]
    assert body["commit"] == NEWER_COMMIT

    installed = await client.post(
        "/skills/ecosystem/install",
        json={"source": body["source_text"], "commit": NEWER_COMMIT, "role": "summarist"},
    )
    assert installed.status_code == 201, installed.text
    assert installed.json()["status"] == "NEEDS_REVIEW", "more permissions: the owner reviews"
    after = next(
        u for u in (await client.get("/skills/updates")).json() if u["skill_id"] == view["skill_id"]
    )
    assert after["state"] == "current" and after["installed_commit"] == NEWER_COMMIT


async def test_a_role_written_in_words_is_accepted_as_its_name(
    client: httpx.AsyncClient,
) -> None:
    """The owner's report: creating an agent "as slac communication assistant"
    was refused five times with a regular expression for an explanation."""
    made = await client.post(
        "/agents",
        json={"name": "Slac", "role": "  Slac communication assistant ", "instructions": "x"},
    )
    assert made.status_code == 201, made.text
    assert made.json()["role"] == "slac-communication-assistant"
    odd = await client.post("/agents", json={"name": "Odd", "role": "2 Tier / Support!"})
    assert odd.status_code == 201 and odd.json()["role"] == "tier-support"
    nothing = await client.post("/agents", json={"name": "None", "role": "!!!"})
    assert nothing.status_code == 422
