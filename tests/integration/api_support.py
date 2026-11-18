"""Helpers and stand-ins shared by the API tests (split from test_api.py)."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import update

from app.db.models import Artifact as ArtifactRow
from app.db.models import Run as RunRow


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    return url


TOKEN = "owner-fixture-token-of-sufficient-length"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def open_approval(client: httpx.AsyncClient, tmp: Any = None) -> tuple[str, str]:
    """Create a run and a pending approval directly, without executing agents."""
    from uuid import uuid4 as _uuid4

    from app.approvals import ApprovalScope

    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()
    store = client._transport.app.state.approvals  # type: ignore[attr-defined]
    scope = ApprovalScope(
        run_id=run["id"],
        project_id=project_id,
        objective="Assess demand",
        recommendation="Build a narrow prototype",
        proposed_engineering_scope="Add an export endpoint",
        permitted_paths=["app/export.py"],
    )
    approval_id = store.open(
        _uuid4().__class__(run["id"]), _uuid4().__class__(run["root_task_id"]), scope
    )
    return str(approval_id), scope.digest()


# What a run that got past planning holds in its state.
_PLANNED = {
    "objective": "o",
    "task_kind": "research_summary",
    "research_question": "q",
    "acceptance_criteria": ["c"],
    "expected_artifacts": ["report"],
}


async def stopped_run(
    client: httpx.AsyncClient, status: str = "NEEDS_HUMAN_REVIEW", researched: bool = True
) -> str:
    """A run stopped for review. Stopped at its revision limit, it has research;
    stopped at planning, it has none."""

    from app.db.models import Run

    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        session.execute(
            update(Run)
            .where(Run.id == UUID(run["id"]))
            .values(
                status=status,
                error="revision_limit_reached" if researched else "needs_owner_answers",
                state_json={"plan": _PLANNED, "research": {"key_findings": ["f"]}}
                if researched
                else {"plan": _PLANNED},
            )
        )
    return str(run["id"])


PROVIDER_KEY = "sk-provider-fixture-value-0123456789"


async def new_connection(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {"provider": "openai", "display_name": "Work", "model_id": "gpt-5"} | overrides
    response = await client.post("/connections", json=body)
    assert response.status_code == 201
    return dict(response.json())


class FakeAdapter:
    """Stands in for a provider; records the credential it was given."""

    provider = "openai"

    def __init__(self, models: list[str], failure: str | None = None) -> None:
        self.models = models
        self.failure = failure
        self.credentials: list[str] = []
        self.billable_requests = 0

    async def list_models(self, credential: str) -> list[str]:
        from app.providers import ProviderFailure

        self.credentials.append(credential)
        if self.failure:
            raise ProviderFailure(self.failure)
        return self.models

    async def test_request(self, credential: str, model_id: str) -> Any:
        from app.providers import Usage

        self.billable_requests += 1
        return Usage(8, 1)


async def stored_connection(client: httpx.AsyncClient) -> str:
    connection = await new_connection(client)
    await client.put(f"/connections/{connection['id']}/credential", json={"secret": PROVIDER_KEY})
    return str(connection["id"])


async def checked_connection(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, name: str = "Work"
) -> str:
    monkeypatch.setattr(
        "app.api.routers.reach.adapter_for", lambda provider, base_url=None: FakeAdapter(["gpt-5"])
    )
    connection = await new_connection(client, display_name=name)
    path = f"/connections/{connection['id']}"
    await client.put(f"{path}/credential", json={"secret": PROVIDER_KEY})
    assert (await client.post(f"{path}/test", json={})).json()["status"] == "MODEL_AVAILABLE"
    return str(connection["id"])


async def new_agent(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {"name": "Researcher", "role": "researcher", "instructions": "Cite sources."}
    response = await client.post("/agents", json=body | overrides)
    assert response.status_code == 201, response.text
    return dict(response.json())


OWNER_TOKEN = "owner-fixture-token-of-sufficient-length"
NO_BEARER = {"Authorization": ""}
SAME_ORIGIN = {"Authorization": "", "Origin": "http://test"}


async def open_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/session", json={"token": OWNER_TOKEN}, headers=NO_BEARER)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


async def project_with_work(
    client: httpx.AsyncClient, name: str, pr_url: str | None = None
) -> tuple[str, Any]:
    from sqlalchemy import update as sql_update

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": name})).json()
    dispatch = state.repository.create(UUID(project["id"]), f"{name} objective")
    state.repository.record_stage(dispatch, "synthesis", {"recommendation": f"{name} rec"})
    if pr_url:
        state.repository.record_stage(
            dispatch, "engineering", {"status": "completed", "pull_request_url": pr_url}
        )
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(RunRow)
            .where(RunRow.id == dispatch.run_id)
            .values(status="COMPLETED", state_json={"plan": _PLANNED})
        )
    return project["id"], dispatch


class RoutingJudge:
    def __init__(self, target: tuple[str, float] = ("strategy", 0.9)) -> None:
        self.target = target
        self.route_answer: dict[str, tuple[str, float]] = {}

    async def revision_target(self, note: str) -> tuple[str, float]:
        return self.target

    async def route(self, text: str, projects: list[str]) -> dict[str, tuple[str, float]]:
        return self.route_answer


def b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


class FakeIndexer:
    """Stands in for the sources service; returns what index_root would."""

    def __init__(self) -> None:
        self.roots: list[str] = []
        self.documents = [
            {
                "path": "notes.txt",
                "sha256": "a" * 64,
                "size_bytes": 40,
                "modified_at": "2026-09-01T00:00:00+00:00",
                "text": "Customer interviews: pricing is confusing.",
            }
        ]
        self.error: str | None = None

    async def folders(self) -> list[str]:
        return ["finance", "research"]

    async def index_staged(self, directory: str) -> dict[str, Any]:
        """Records what the API staged, then answers as the reader would."""
        self.staged_dir = directory
        staging = Path(os.environ["TEST_STAGING"]) / directory
        self.staged = sorted(
            p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()
        )
        return {
            "documents": [
                {
                    "path": name,
                    "sha256": "b" * 64,
                    "size_bytes": 10,
                    "modified_at": None,
                    "text": f"text of {name}",
                }
                for name in self.staged
            ],
            "skipped": [],
            "truncated": False,
            "error": self.error,
        }

    async def index(self, root: str) -> dict[str, Any]:
        self.roots.append(root)
        return {
            "documents": list(self.documents),
            "skipped": [{"path": "scan.pdf", "reason": "unsupported_format"}],
            "truncated": False,
            "error": self.error,
        }


async def granted_project(
    client: httpx.AsyncClient, indexer: FakeIndexer, name: str, text: str
) -> tuple[str, str]:
    indexer.documents = [indexer.documents[0] | {"path": f"{name}.md", "text": text}]
    source = (await client.post("/sources", json={"name": name, "root": name})).json()
    project = (await client.post("/projects", json={"name": f"{name} project"})).json()
    await client.put(f"/projects/{project['id']}/sources/{source['id']}")
    return project["id"], source["id"]


class FakeDirect:
    made: list[dict[str, Any]] = []

    def __init__(self, provider: str, model_id: str, credential: str, settings: Any, label: str):
        FakeDirect.made.append({"provider": provider, "model": model_id, "credential": credential})
        self.fail = FakeDirect.fail_with

    fail_with: str | None = None

    async def complete(self, alias: str, messages: list[dict[str, str]], schema: Any = None) -> Any:
        from app.gateway.client import Completion, GatewayError, Usage

        if self.fail:
            raise GatewayError(self.fail)
        assert "Cite sources." in messages[0]["content"]
        return Completion(
            model_alias=alias,
            model="gpt-5-2026",
            content="I research and cite sources.",
            usage=Usage(prompt_tokens=40, completion_tokens=8),
            latency_seconds=0.4,
        )


async def new_schedule(client: httpx.AsyncClient, **rule: Any) -> tuple[dict[str, Any], str]:
    project = (await client.post("/projects", json={"name": f"Sched {uuid4().hex[:6]}"})).json()
    template = (
        await client.post(
            "/templates",
            json={
                "name": f"T {uuid4().hex[:6]}",
                "project_id": project["id"],
                "objective": "Brief",
            },
        )
    ).json()
    body = {
        "template_id": template["id"],
        "timezone": "Europe/Amsterdam",
        "frequency": "daily",
        "local_time": "09:00",
    } | rule
    response = await client.post("/schedules", json=body)
    assert response.status_code == 201, response.text
    return response.json(), project["id"]


class FakeSlack:
    kind = "slack"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.threads: list[str | None] = []
        # What the platform itself would show when asked.
        self.history: list[str] = []
        self.fail_with: str | None = None
        # Buttons, edits in place and files.
        self.blocks: list[Any] = []
        self.updates: list[tuple[str, str, str]] = []
        self.uploads: list[tuple[str, str | None, str, bytes]] = []
        self.upload_fails_with: str | None = None
        # Messages a 💡 can be put on, and what was whispered.
        self.messages: dict[tuple[str, str], str] = {}
        self.whispers: list[tuple[str, str, str]] = []

    async def message_text(self, token: str, channel: str, ts: str) -> str:
        from app.channels.base import ChannelError

        if (channel, ts) not in self.messages:
            raise ChannelError("missing_scope")
        return self.messages[(channel, ts)]

    async def whisper(self, token: str, channel: str, user: str, text: str) -> None:
        self.whispers.append((channel, user, text))

    def authorize_url(self, client_id: str, redirect_uri: str, state: str) -> str:
        from app.channels.slack import SlackAdapter

        return SlackAdapter().authorize_url(client_id, redirect_uri, state)

    async def exchange_code(
        self, code: str, client_id: str, client_secret: str, redirect_uri: str
    ) -> Any:
        from app.channels.base import Installation

        assert client_secret == "client-secret-fixture-value"
        return Installation(
            "T0TEAM", "Northwind", "U0BOT", "xoxb-oauth-token-fixture", ("chat:write",), "U0OWNER"
        )

    async def verify_token(self, token: str) -> Any:
        from app.channels.base import ChannelError, Installation

        if not token.startswith("xoxb-"):
            raise ChannelError("invalid_auth")
        # Each Slack app has its own bot user; the office bot's token is BOT.
        bot = "U0BOT" if token == BOT else f"U0{token.rsplit('-', 1)[-1].upper()}"
        return Installation("T0TEAM", "Northwind", bot, token, ("chat:write",))

    async def list_destinations(self, token: str) -> Any:
        from app.channels.base import Destination

        return [Destination("C1", "#office-updates", True), Destination("C2", "#random", False)]

    async def find_message(
        self, token: str, destination: str, text: str, since: float
    ) -> str | None:
        from app.channels.base import ChannelError

        if self.fail_with:
            raise ChannelError(self.fail_with)
        return f"{destination}:9.9" if text in self.history else None

    async def send(self, token: str, destination: str, message: Any) -> Any:
        from app.channels.base import ChannelError, DeliveryResult

        if self.fail_with:
            raise ChannelError(self.fail_with)
        self.sent.append((token, destination, message.text))
        self.threads.append(message.thread)
        self.blocks.append(message.blocks)
        return DeliveryResult(f"{destination}:1.{len(self.sent)}")

    async def update(self, token: str, destination: str, ts: str, message: Any) -> None:
        self.updates.append((destination, ts, message.text))

    async def upload(
        self, token: str, destination: str, thread: str | None, name: str, data: bytes
    ) -> None:
        from app.channels.base import ChannelError

        if self.upload_fails_with:
            raise ChannelError(self.upload_fails_with)
        self.uploads.append((destination, thread, name, data))


BOT = "xoxb-manual-token-fixture-000"


async def slack_destination(client: httpx.AsyncClient, events: list[str] | None = None) -> str:
    installation = (
        await client.post("/channels/slack/installations", json={"bot_token": BOT})
    ).json()
    destination = await client.post(
        f"/channels/installations/{installation['id']}/destinations",
        json={
            "external_id": "C1",
            "name": "#office-updates",
            "events": events or ["needs_approval", "completed", "failed"],
        },
    )
    assert destination.status_code == 201, destination.text
    return str(destination.json()["id"])


AGENT_BOT = "xoxb-agent-token-fixture-researcher"
AGENT_APP = "xapp-agent-app-token-fixture-001"


async def agent_with_bot(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    agent = await new_agent(client, **overrides)
    response = await client.put(
        f"/agents/{agent['id']}/chat-bot/slack",
        json={"bot_token": AGENT_BOT, "app_token": AGENT_APP},
    )
    assert response.status_code == 200, response.text
    return agent | {"bot": response.json()}


async def asked_in_slack(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, status: str = "COMPLETED"
) -> tuple[Any, UUID]:
    """A finished task, as if the owner had asked for it in a Slack thread."""
    await slack_destination(client)
    [installation] = (await client.get("/channels")).json()["installations"]
    _, run_id = await finished_run(client, monkeypatch)
    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        row = session.get(RunRow, UUID(run_id))
        assert row is not None
        row.status = status
        row.config_snapshot_json = {
            **(row.config_snapshot_json or {}),
            "origin": {
                "kind": "slack",
                "app_id": installation["app_id"],
                "agent_profile_id": None,
                "channel": "D1",
                "thread": "1700.1",
            },
        }
    return state, UUID(run_id)


def store_of(state: Any) -> Any:
    from app.artifacts import ArtifactStore

    return ArtifactStore(state.worker.settings.workspace_root)


class LookupModel:
    """The office's model for a Slack conversation (now the lead, §15): it
    calls a tool, then answers. A reply is answer text, or (tool, arguments) to call."""

    replies: list[Any] = []
    seen: list[Any] = []
    sees = True

    def __init__(self, settings: Any) -> None:
        return None

    async def __aenter__(self) -> "LookupModel":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def complete(self, alias: str, messages: list[Any], **options: Any) -> Any:
        from app.gateway.client import Completion, ToolCall, Usage

        LookupModel.seen.append(list(messages))
        reply = LookupModel.replies.pop(0)
        calls = []
        if isinstance(reply, tuple):
            name, arguments = reply
            calls = [
                ToolCall(
                    id="c1", name=name, arguments=arguments, raw_arguments=json.dumps(arguments)
                )
            ]
            reply = ""
        return Completion(
            model_alias=alias,
            model="m",
            content=reply,
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            latency_seconds=0.0,
            estimated_cost=0.001,
            tool_calls=calls,
        )


async def _map(value: Any) -> Any:
    return value


async def budget_paused_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, budget: dict[str, Any]
) -> str:
    """A run paused at a ceiling, as the dispatcher leaves one."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Ceiling {uuid4().hex[:6]}"})).json()
    run = (
        await client.post(
            "/runs",
            json={"project_id": project["id"], "objective": "Assess demand", "budget": budget},
        )
    ).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    # Real usage rows: what the run actually spent is read from these, not from
    # the question's payload, so a fixture without them proves nothing.
    from app.events import InvocationEvent

    repository.record_usage(
        InvocationEvent(
            run_id=UUID(run["id"]),
            project_id=UUID(project["id"]),
            role="researcher",
            model_alias="0002",
            status="ok",
            prompt_tokens=300000,
            completion_tokens=97284,
            estimated_cost=0.11,
        )
    )
    repository.stop(UUID(run["id"]), "PAUSED")
    repository.add_event(
        UUID(run["id"]),
        "budget_ask",
        {
            "limit": "tokens",
            "node": "critic",
            "budget": budget,
            "spent": {"tokens": 397284, "cost_usd": 0.11, "cost_reported": False, "calls": 13},
        },
    )
    return str(run["id"])


async def artifact_of(client: httpx.AsyncClient, payload: dict[str, Any]) -> str:
    """One stored artifact, as a finished step leaves it."""
    project = (await client.post("/projects", json={"name": f"Doc {uuid4().hex[:6]}"})).json()
    run = (
        await client.post(
            "/runs", json={"project_id": project["id"], "objective": "拟议项目：KYC 调研"}
        )
    ).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    described = repository.describe([UUID(run["id"])])
    dispatch = described[0][0]
    repository.record_stage(dispatch, "research", payload)
    artifacts = (await client.get(f"/runs/{run['id']}/artifacts")).json()
    return str(artifacts[0]["id"])


DOCUMENT_PAYLOAD = {
    "research_question": "Which KYC vendors serve Dutch EMIs?",
    "key_findings": ["Three vendors cover NL"],
    "evidence": ["The AFM guidance says so"],
    "unknowns": ["Pricing is not published"],
    "confidence": "medium",
    "sources": [{"title": "AFM guidance", "url": "https://afm.nl/kyc"}],
}


async def memory_run(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """A finished run whose record supports lessons."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Mem {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "KYC vendors"})
    ).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    dispatch = repository.describe([UUID(run["id"])])[0][0]
    repository.record_stage(
        dispatch,
        "research",
        {
            "research_question": "Which KYC vendors serve Dutch EMIs?",
            "key_findings": ["Something"],
            "evidence": ["Somewhere"],
            "implications": ["Something follows"],
            "confidence": "medium",
            "sources": [{"title": "A", "url": "https://a.example"}],
            "blocked_sources": [{"url": "https://www.dnb.nl/en/", "status": "http_403"}],
            "answers": [
                {
                    "question": "Which vendors support Dutch eID?",
                    "status": "unanswered",
                    "answer": "Only marketing was retrieved.",
                    "missing": ["Vendor documentation"],
                }
            ],
        },
    )
    return str(project["id"]), str(run["id"])


async def venture_and_projects(client: httpx.AsyncClient) -> tuple[str, str, str, str]:
    """One business line with two projects, and one project outside it."""
    fintech = (
        await client.post(
            "/ventures",
            json={
                "name": f"Fintech {uuid4().hex[:6]}",
                "doctrine": "A regulatory claim must cite a primary instrument by article.",
            },
        )
    ).json()
    inside_a = (await client.post("/projects", json={"name": f"EMI {uuid4().hex[:6]}"})).json()
    inside_b = (await client.post("/projects", json={"name": f"KYC {uuid4().hex[:6]}"})).json()
    outside = (await client.post("/projects", json={"name": f"Other {uuid4().hex[:6]}"})).json()
    for project in (inside_a, inside_b):
        assert (
            await client.put(
                f"/projects/{project['id']}/venture", json={"venture_id": fintech["id"]}
            )
        ).status_code == 200
    return fintech["id"], inside_a["id"], inside_b["id"], outside["id"]


async def finished_run(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """A project and one task that has concluded something."""
    import json

    from app.artifacts import ArtifactMetadata

    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Thread {uuid4().hex[:6]}"})).json()
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": (
                    "Assess whether we can apply for the EMI licence this quarter, so that "
                    "the board can decide in October."
                ),
            },
        )
    ).json()

    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    store = client.app_state.worker.repository.store  # type: ignore[attr-defined]
    with factory() as session:
        row = session.get(RunRow, UUID(run["id"]))
        assert row is not None
        row.status = "COMPLETED"
        metadata = ArtifactMetadata(
            project_id=UUID(project["id"]),
            task_id=row.root_task_id,
            run_id=row.id,
            type="synthesis",
            created_by="strategist",
        )
        store.write(
            metadata,
            json.dumps(
                {
                    "recommendation": "Apply this quarter; the gap is governance, not capital.",
                    "unknowns": ["No provider publishes its onboarding time."],
                }
                | (extra or {})
            ),
        )
        session.add(
            ArtifactRow(
                id=metadata.artifact_id,
                project_id=UUID(project["id"]),
                task_id=row.root_task_id,
                run_id=row.id,
                type="synthesis",
                path=str(store.path_of(metadata)),
                created_by="strategist",
            )
        )
        session.commit()
    return project["id"], run["id"]


async def building_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, UUID, Path]:
    """A task whose build is being written now (LB): an attempt clone and its steps."""
    _, run_id = await finished_run(client, monkeypatch)
    state = client.app_state  # type: ignore[attr-defined]
    clone = state.worker.settings.engineering_root / "attempts" / uuid4().hex / "repo"
    clone.mkdir(parents=True)
    (clone / "index.html").write_text("<html><body><h1>Colouring</h1></body></html>")
    with state.session_factory() as session, session.begin():
        row = session.get(RunRow, UUID(run_id))
        assert row is not None
        row.status = "RUNNING"
        row.state_json = {**(row.state_json or {}), "build_folder": "site-live"}
    for step in (
        {"kind": "workspace", "path": str(clone)},
        {"kind": "phase", "text": "Codex is writing it, in a sandbox."},
        {"kind": "session", "id": "thread-1"},
        {"kind": "plan", "items": [{"text": "Write the page", "done": True}]},
        {"kind": "file", "text": "Created index.html", "path": "index.html", "diff": "+<h1>"},
        {"kind": "now", "text": "Running node --check app.js"},
    ):
        state.repository.add_event(UUID(run_id), "build_step", step)
    return state, UUID(run_id), clone


DASHBOARD = {
    "lead": "Sign-ups tripled this year.",
    "deliverable_body": [
        {
            "heading": "Sign-ups by quarter",
            "points": ["Up every quarter."],
            "table": {
                "columns": ["Quarter", "Sign-ups"],
                "rows": [["Q1 2025", "100"], ["Q2 2025", "180"], ["Q3 2025", "300"]],
            },
        }
    ],
}


async def _granted_filings(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, dict]:
    """A reviewed Filings server granted to a new project: search allowed, file_issue asked."""
    from app.mcp.client import OfferedTool, Offering

    async def offering(self: Any, url: str, token: str | None, allow: list[str]) -> Offering:
        return Offering(
            "Filings",
            "1",
            "x",
            [
                OfferedTool("search", "Search a filing index.", {}, {}),
                OfferedTool("file_issue", "Open an issue.", {}, {}),
            ],
        )

    monkeypatch.setattr("app.mcp.client.McpClient.offering", offering)
    server = (
        await client.post(
            "/mcp/servers",
            json={"name": f"Filings {uuid4().hex[:6]}", "url": "https://mcp.example.com/mcp"},
        )
    ).json()
    for tool, stance in (("search", "allowed"), ("file_issue", "needs_approval")):
        await client.post(
            f"/mcp/servers/{server['id']}/tools/{tool}/decision", json={"stance": stance}
        )
    project = (await client.post("/projects", json={"name": f"Tools {uuid4().hex[:6]}"})).json()
    await client.put(f"/projects/{project['id']}/mcp/{server['id']}")
    return server, project


# Agents reply in the room.


class FakeReplies:
    """Stands in for each agent's model: records what each was asked."""

    def __init__(self, answers: dict[str, str] | None = None, cost: float = 0.01) -> None:
        self.answers = answers or {}
        self.cost = cost
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, agent: dict[str, Any], messages: list[dict], **options: Any) -> Any:
        from app.gateway.client import Completion, Usage

        prompt = "\n".join(message["content"] for message in messages)
        self.asked.append((agent["name"], prompt))
        return Completion(
            model_alias="fake",
            model="fake",
            content=self.answers.get(agent["name"], f"{agent['name']} here."),
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            latency_seconds=0.1,
            estimated_cost=self.cost,
        )


async def _room_with_team(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, str] | None = None,
) -> tuple[str, FakeReplies]:
    fake = FakeReplies(answers)
    monkeypatch.setattr("app.room_replies.RoomReplies.complete", fake)
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    for name, role in (
        ("Iris", "researcher"),
        ("Cato", "chief"),
        ("Vera", "critic"),
        ("Wren", "summarist"),
    ):
        await new_agent(client, name=name, role=role)
    project = (await client.post("/projects", json={"name": f"Talk {uuid4().hex[:6]}"})).json()
    return project["id"], fake


async def _said(client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in (await client.get(f"/projects/{project_id}/room")).json()["items"]
        if item["kind"] == "message"
    ]


# Talk becomes work, by a button.


async def _proposed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, Any], list[Any]]:
    """A room where Iris has offered a task. Returns the project, the reply, and
    what the worker was handed."""
    answer = "I could not read DNB.\nPROPOSE: Read DNB's Q&A on simplified CDD."
    project_id, _ = await _room_with_team(client, monkeypatch, {"Iris": answer})
    submitted: list[Any] = []
    monkeypatch.setattr(client.app_state.worker, "submit", submitted.append)  # type: ignore[attr-defined]
    await client.post(f"/projects/{project_id}/room", json={"text": "@Iris why not DNB?"})
    reply = (await _said(client, project_id))[-1]
    return project_id, reply, submitted


def _action_path(project_id: str, message: dict[str, Any], index: int = 0) -> str:
    return (
        f"/projects/{project_id}/room/messages/{message['id'].removeprefix('m:')}/actions/{index}"
    )


# What the project's tasks are waiting on, in the room.


async def _waiting(client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
    return list((await client.get(f"/projects/{project_id}/room")).json()["waiting"])


# Slack remembers open proposals and thread history across a restart.


def _slack_handler(state: Any, office: Any, app_id: UUID) -> Any:
    from app.channels.inbound import InboundHandler, Pairing
    from app.channels.threads import DatabaseThreads
    from tests.unit.test_inbound import Replies

    return InboundHandler(
        office, Replies(), Pairing(), DatabaseThreads(state.session_factory, app_id)
    )


# Signing in to a tool server with the owner's account.


def _asks_for_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp.client import McpError

    async def offering(self: Any, url: str, token: str | None, allow: list[str]) -> Any:
        if token is None:
            raise McpError("sign_in_required", 'Bearer resource_metadata="https://x/prm"')
        from app.mcp.client import OfferedTool, Offering

        assert token == "at-1", "the access token reaches the server"
        return Offering("Notes", "1", "x", [OfferedTool("search_notes", "Search notes.", {}, {})])

    monkeypatch.setattr("app.mcp.client.McpClient.offering", offering)


def _vendor(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]) -> None:
    from app.mcp import oauth

    server = oauth.AuthServer(
        "https://auth.example.com",
        "https://auth.example.com/authorize",
        "https://auth.example.com/token",
        "https://auth.example.com/register",
        "https://mcp.example.com/mcp",
        ("read",),
    )

    async def discover(url: str, challenge: str, allow: list[str], client: Any = None) -> Any:
        seen["challenge"] = challenge
        return server

    async def register(found: Any, redirect: str, allow: list[str], client: Any = None) -> Any:
        seen["redirect"] = redirect
        return oauth.Registration("office-client", None, "none")

    async def exchange(
        found: Any,
        reg: Any,
        code: str,
        verifier: str,
        redirect: str,
        allow: Any,
        client: Any = None,
    ) -> str:
        seen["code"] = code
        return oauth.credential(
            "at-1", "rt-1", None, reg, found.token_endpoint, found.resource, "read"
        )

    monkeypatch.setattr("app.mcp.oauth.discover", discover)
    monkeypatch.setattr("app.mcp.oauth.register", register)
    monkeypatch.setattr("app.mcp.oauth.exchange", exchange)


# Paid tool calls count against the limits.


async def _priced_filings(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, price: float
) -> tuple[dict, dict]:
    server, project = await _granted_filings(client, monkeypatch)
    decided = await client.post(
        f"/mcp/servers/{server['id']}/tools/search/decision",
        json={"stance": "allowed", "price_usd": price},
    )
    assert decided.status_code == 200, decided.text
    search = next(tool for tool in decided.json()["tools"] if tool["name"] == "search")
    assert search["price_usd"] == price
    return server, project


def _answers(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    from app.mcp.client import ToolResult

    async def call(
        self: Any, url: str, token: Any, allow: Any, name: str, arguments: dict
    ) -> ToolResult:
        sent.append(name)
        return ToolResult(content="a filing", failed=False)

    monkeypatch.setattr("app.mcp.client.McpClient.call", call)


class _RelevanceJudge:
    """Scores a passage 3 when it mentions any of `about`, else 0."""

    def __init__(self, *about: str, fail: bool = False) -> None:
        self.about = about
        self.fail = fail
        self.asked: list[str] = []

    async def relevance(self, question: str, passages: list[str]) -> list[float]:
        from app.judgments import JudgmentUnavailable

        if self.fail:
            raise JudgmentUnavailable("typesafe_unavailable")
        self.asked += passages
        return [3.0 if any(word in p for word in self.about) else 0.0 for p in passages]


async def _finished(client: httpx.AsyncClient, name: str, objective: str, answer: str) -> Any:
    from sqlalchemy import update as sql_update

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": name})).json()
    dispatch = state.repository.create(UUID(project["id"]), objective)
    state.repository.record_stage(dispatch, "synthesis", {"recommendation": answer})
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(RunRow)
            .where(RunRow.id == dispatch.run_id)
            .values(status="COMPLETED", completed_at=datetime.now(UTC))
        )
    return dispatch


# Skills from the open ecosystem.


def _found(commit: str = "c" * 40, name: str | None = None) -> Any:
    from app.skills.ecosystem import Found

    return Found(
        owner="acme",
        repo="skills",
        path="skills/to-questionnaire/SKILL.md",
        commit=commit,
        name=name or f"to-questionnaire-{uuid4().hex[:6]}",
        description="Turn a gap into a questionnaire.",
        instructions="Ask who it goes to. " * 600,  # longer than a catalog skill may be
        refused=["scripts/fill.py"],
        assumptions=[{"kind": "files", "text": "It writes files."}],
        audits={"gen": "Pass", "socket": "Pass", "snyk": "Warn"},
        suggested_role="summarist",
    )


# Progress, spend and what needs you, at a glance.


def _spend(state: Any, dispatch: Any, cost: float, tokens: int = 100) -> None:
    from app.events import InvocationEvent

    state.repository.record_usage(
        InvocationEvent(
            run_id=dispatch.run_id,
            task_id=dispatch.task_id,
            project_id=dispatch.project_id,
            role="researcher",
            model_alias="m",
            status="ok",
            prompt_tokens=tokens,
            completion_tokens=0,
            estimated_cost=cost,
        )
    )


def _set_run(state: Any, run_id: UUID, **values: Any) -> None:
    from sqlalchemy import update as sql_update

    with state.session_factory() as session, session.begin():
        session.execute(sql_update(RunRow).where(RunRow.id == run_id).values(**values))


async def _pulse(client: httpx.AsyncClient) -> dict:
    response = await client.get("/office/pulse")
    assert response.status_code == 200, response.text
    return response.json()


# Business lines sorted on their own.


class LineJudge:
    """Answers by project name, and remembers who it was asked about."""

    def __init__(self, answers: dict[str, tuple[str, float]]) -> None:
        self.answers = answers
        self.asked: list[list[str]] = []

    async def business_lines(
        self, projects: list[dict[str, Any]], lines: list[tuple[str, str]]
    ) -> list[tuple[str, float]]:
        self.asked.append([p["name"] for p in projects])
        return [self.answers.get(p["name"], ("none", 0.9)) for p in projects]


async def _line_office(client: httpx.AsyncClient) -> tuple[str, dict[str, Any], dict[str, Any]]:
    tag = uuid4().hex[:6]
    line = (
        await client.post("/ventures", json={"name": f"Fintech {tag}", "description": "EMI"})
    ).json()
    sure = (await client.post("/projects", json={"name": f"EMI governance {tag}"})).json()
    unsure = (await client.post("/projects", json={"name": f"Misc {tag}"})).json()
    return tag, line, {"sure": sure, "unsure": unsure}


async def _auto_sort(client: httpx.AsyncClient, judge: LineJudge) -> dict[str, Any]:
    from app.venture_routing import auto_sort

    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        return await auto_sort(session, judge)


# Limit warnings reach Slack.


def _crossings(state: Any, settings: Any) -> None:
    """The scheduler's pass, as it runs every tick with nobody looking."""
    from app.scheduler import limit_pass

    limit_pass(state.session_factory, settings)


# Skill updates, and packs per business line.

NEWER_COMMIT = "d" * 40


async def _installed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, versions: dict[str, Any]
) -> dict[str, Any]:
    """Install a skill whose repository holds `versions` by commit."""

    async def fetch(owner: str, repo: str, skill: Any, allow: Any, commit: Any = None) -> Any:
        return versions[commit or "c" * 40]

    monkeypatch.setattr("app.skills.ecosystem.fetch", fetch)
    body = {"source": "acme/skills to-questionnaire", "commit": "c" * 40, "role": "summarist"}
    made = await client.post("/skills/ecosystem/install", json=body)
    assert made.status_code == 201, made.text
    return dict(made.json())


def _changed(monkeypatch: pytest.MonkeyPatch, newer: bool = True, fails: str | None = None) -> None:
    from app.skills.ecosystem import EcosystemError

    async def latest_change(
        owner: str, repo: str, folder: str, installed: str, allow: Any, client: Any = None
    ) -> dict[str, Any]:
        if fails:
            raise EcosystemError(fails)
        assert folder == "skills/to-questionnaire", folder
        return {
            "commit": NEWER_COMMIT if newer else installed,
            "date": "2026-09-28T10:00:00Z",
            "message": "Tighten the questions",
            "newer": newer,
        }

    monkeypatch.setattr("app.skills.ecosystem.latest_change", latest_change)


# TR: one room. Huddles summarised in the project room; asking about a task.


def _team_post(
    state: Any, run_id: Any, key: str, kind: str, role: str, text: str, **rest: Any
) -> None:
    state.repository.add_team_post(
        run_id,
        {
            "key": key,
            "kind": kind,
            "author_role": role,
            "text": text,
            "phase": "huddle",
            "wave": 1,
            **rest,
        },
    )


# CH: the room as a chat. Reply to one message; threads.


async def _message(client: httpx.AsyncClient, project_id: str, author: str) -> dict[str, Any]:
    return next(m for m in await _said(client, project_id) if m["author"]["name"] == author)


# From talk to work, and back. What a message commits to is judged; the
# office acts on it in the topic.


class _Commitment:
    """Stands in for Jev's reading of what a message commits to."""

    def __init__(self, kind: str, confidence: float = 0.9) -> None:
        self.kind = kind
        self.confidence = confidence
        self.read: list[str] = []

    async def commitment(self, text: str, recent: list[str]) -> tuple[str, float]:
        self.read.append(text)
        return self.kind, self.confidence


async def _topic_with(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, kind: str, confidence: float = 0.9
) -> tuple[dict[str, Any], FakeReplies, _Commitment]:
    _, fake = await _room_with_team(client, monkeypatch)
    judge = _Commitment("talk")
    monkeypatch.setattr("app.handoff.judge_for", lambda settings: judge)
    started = (
        await client.post("/topics", json={"text": "Pricing for a first paid service"})
    ).json()
    fake.asked.clear()
    judge.kind, judge.confidence = kind, confidence
    return started["topic"], fake, judge


async def _said_in(client: httpx.AsyncClient, topic_id: str) -> list[dict[str, Any]]:
    feed = (await client.get(f"/topics/{topic_id}/feed")).json()
    return [i for i in feed["items"] if i["kind"] == "message"]


async def _offers(client: httpx.AsyncClient, topic_id: str) -> list[dict[str, Any]]:
    """The office's offers to start work, in the order they were made."""
    return [
        m
        for m in await _said_in(client, topic_id)
        if m["author"]["kind"] == "office"
        and any(a.get("kind") == "start_work" for a in m.get("actions") or [])
    ]


async def _hand_off(client: httpx.AsyncClient, topic_id: str, text: str) -> dict[str, Any]:
    """Ask for work and press Start on the office's offer, as the owner does."""
    sent = (await client.post(f"/topics/{topic_id}/messages", json={"text": text})).json()
    offer = (await _offers(client, topic_id))[-1]
    message_id = offer["id"].removeprefix("m:")
    pressed = await client.post(f"/topics/{topic_id}/messages/{message_id}/actions/0", json={})
    assert pressed.status_code == 201, pressed.text
    return sent


# Colleagues, branching, and coming back.


async def _agent_id(client: httpx.AsyncClient, name: str) -> str:
    agents = (await client.get("/agents")).json()
    return next(a["id"] for a in agents if a["name"] == name)


BRIEF = (
    '{"where": "Mia is comparing five diagnostic services.", '
    '"decided": "Start with a human-delivered diagnosis.", '
    '"open": "Include a follow-up call?"}'
)


def _agent_says(client: httpx.AsyncClient, topic_id: str, reply_to: str, text: str) -> str:
    """An agent's message in the discussion, answering `reply_to`."""
    from app.db.models import RoomMessage

    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        said = RoomMessage(
            topic_id=UUID(topic_id),
            author_kind="agent",
            author_name="Iris",
            body=text,
            reply_to=int(reply_to.removeprefix("m:")),
        )
        session.add(said)
        session.flush()
        return f"m:{said.id}"


# What the office brings back, sorted. Needs you holds only decisions;
# deliveries are to glance at; ideas and returning discussions are parked.


async def _work_in(client: httpx.AsyncClient, topic_id: str, status: str) -> UUID:
    from sqlalchemy import select as sql_select
    from sqlalchemy import update as sql_update

    state = client.app_state  # type: ignore[attr-defined]
    # The work the latest message started: two started in the same second
    # list in either order in the feed.
    with state.session_factory() as session:
        run_id = session.scalars(
            sql_select(RunRow.id)
            .where(RunRow.topic_id == UUID(topic_id))
            .order_by(RunRow.origin_message_id.desc())
        ).first()
    assert run_id is not None
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=status, completed_at=datetime.now(UTC))
        )
    return run_id


# The shared workspace. The delivered document beside the discussion; a
# passage commented on is revised into a new version, with a note of what changed.


async def _delivered_in(client: httpx.AsyncClient, topic_id: str, answer: dict[str, Any]) -> UUID:
    state = client.app_state  # type: ignore[attr-defined]
    await _hand_off(client, topic_id, "Compare the prices.")
    run_id = await _work_in(client, topic_id, "COMPLETED")
    described = state.repository.describe([run_id])[0][0]
    state.repository.record_stage(described, "synthesis", answer)
    return run_id


ANSWER = {
    "recommendation": "Start with a human-delivered diagnosis.",
    "summary": "A typical engagement costs about EUR 5,000.",
    "key_findings": ["Three of five publish prices."],
}


# A backup per provider, and a card for each.


def _connection(client: httpx.AsyncClient, name: str = "DeepSeek") -> str:
    from app.db.models import ModelConnection

    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        row = ModelConnection(
            provider="deepseek",
            display_name=name,
            model_id="deepseek-flash",
            credential_ref=f"conn-{uuid4().hex}",
            status="MODEL_AVAILABLE",
        )
        session.add(row)
        session.flush()
        return str(row.id)


# The owner edits the answer in place; a passage can be rewritten on request.


def _edited(document: dict[str, Any], text: str) -> dict[str, Any]:
    """The answer's own sections, with the first paragraph replaced."""
    sections = [row for row in document["sections"] if not row["supporting"]]
    sections[0]["blocks"][0] = {"kind": "paragraph", "text": text}
    return {"title": document["title"], "sections": sections}


# Decisions only from the owner, with their source and what they replace;
# business spaces; related work within its business line.


async def _line_with_project(client: httpx.AsyncClient, line: str, project: str) -> tuple[str, str]:
    from sqlalchemy import update as sql_update

    from app.db.models import Project

    state = client.app_state  # type: ignore[attr-defined]
    venture = (await client.post("/ventures", json={"name": f"{line} {uuid4().hex[:5]}"})).json()
    made = (await client.post("/projects", json={"name": f"{project} {uuid4().hex[:5]}"})).json()
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(Project)
            .where(Project.id == UUID(made["id"]))
            .values(venture_id=UUID(venture["id"]), venture_set_by="owner")
        )
    return venture["id"], made["id"]


# One entry point, and signing in a phone.


def _verified(client: httpx.AsyncClient, kind: str, address: str) -> None:
    from app.db.models import AccessPoint

    state = client.app_state  # type: ignore[attr-defined]
    with state.session_factory() as session, session.begin():
        found = session.get(AccessPoint, kind) or AccessPoint(kind=kind)
        found.address = address
        found.enabled = True
        found.verified_at = datetime.now(UTC)
        found.check_json = {}
        session.merge(found)
    state.reachable.refresh()


class FakeRemote:
    """The remote container, as the office sees it through the exchange."""

    def __init__(self) -> None:
        self.status: dict[str, Any] | None = None
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, dict[str, Any]] = {}

    def seen(self) -> dict[str, Any] | None:
        return self.status

    async def ask(self, action: str, **body: Any) -> dict[str, Any]:
        self.asked.append((action, body))
        answer = self.answers.get(action, {"ok": True})
        if self.status is not None:
            self.status = {**self.status, **{k: v for k, v in answer.items() if k != "said"}}
        return answer


def _remote_stage(points: dict[str, Any]) -> str:
    return str(points["points"][1]["remote"]["stage"])


# 4 Oct: what the agents are told when they answer. They could not see the
# answer of the discussion's own (cancelled) work, did not know their
# colleagues, and knew nothing of work asked in another discussion.


async def _asked_last(
    client: httpx.AsyncClient, topic_id: str, fake: FakeReplies, text: str
) -> str:
    fake.asked.clear()
    await client.post(f"/topics/{topic_id}/messages", json={"text": text})
    assert fake.asked, "an agent answered"
    return fake.asked[-1][1]


def calendar_link_server(client: httpx.AsyncClient, text: str, status: int = 200) -> None:
    """The office's calendars read this instead of the internet."""
    calendars = client.app_state.calendars  # type: ignore[attr-defined]
    calendars.http = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=text))
    )


def calendar_today(title: str) -> str:
    now = datetime.now(UTC)
    start = now - timedelta(minutes=10)
    end = now + timedelta(minutes=50)
    stamp = "%Y%m%dT%H%M%SZ"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\nBEGIN:VEVENT\r\nUID:now\r\n"
        f"SUMMARY:{title}\r\nDTSTART:{start.strftime(stamp)}\r\nDTEND:{end.strftime(stamp)}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def calendar_soon(title: str) -> str:
    now = datetime.now(UTC)
    start = now + timedelta(hours=1)
    stamp = "%Y%m%dT%H%M%SZ"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\nBEGIN:VEVENT\r\nUID:soon\r\n"
        f"SUMMARY:{title}\r\nDESCRIPTION:Dial-in 4242\r\nDTSTART:{start.strftime(stamp)}\r\n"
        f"DTEND:{(start + timedelta(hours=1)).strftime(stamp)}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class FakeCompany:
    """Google's or Microsoft's token and calendar servers, as the office meets them."""

    def __init__(self, scope: str = "") -> None:
        self.forms: list[dict[str, list[str]]] = []
        soon = datetime.now(UTC) + timedelta(hours=1)
        self.when = soon.replace(microsecond=0)
        self.scope = scope
        self.writes: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/token"):
            self.forms.append(parse_qs(request.content.decode()))
            tokens = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
            return httpx.Response(200, json=tokens | ({"scope": self.scope} if self.scope else {}))
        assert request.headers["authorization"] == "Bearer at-1"
        if request.method in ("POST", "PATCH", "DELETE"):
            body = json.loads(request.content) if request.content else None
            self.writes.append((request.method, url, body))
            return httpx.Response(
                204 if request.method == "DELETE" else 200,
                json={} if request.method != "DELETE" else None,
            )
        if "/users/me/calendarList" in url:
            items = [
                {
                    "id": "me@gmail.com",
                    "summary": "Me",
                    "primary": True,
                    "accessRole": "owner",
                    "backgroundColor": "#1a73e8",
                },
                {"id": "holidays", "summary": "Holidays", "accessRole": "reader"},
            ]
            return httpx.Response(200, json={"items": items})

        if "googleapis.com/calendar/v3/calendars/" in url:
            start = self.when.isoformat()
            end = (self.when + timedelta(hours=1)).isoformat()
            item = {
                "id": "e1",
                "summary": "Investor call",
                "start": {"dateTime": start},
                "end": {"dateTime": end},
                "description": "PIN 9911",
            }
            return httpx.Response(200, json={"items": [item]})
        if url.startswith("https://graph.microsoft.com/v1.0/me?"):
            return httpx.Response(200, json={"mail": "me@outlook.com"})
        if "/me/calendars?" in url:
            listing = [{"id": "cal-1", "name": "Calendar", "canEdit": True, "hexColor": "#0078d4"}]
            return httpx.Response(200, json={"value": listing})
        if "/calendarView" in url:
            assert request.headers["prefer"] == 'outlook.timezone="UTC"'
            stamp = "%Y-%m-%dT%H:%M:%S.0000000"
            item = {
                "id": "m1",
                "subject": "Standup",
                "start": {"dateTime": self.when.strftime(stamp)},
                "end": {"dateTime": (self.when + timedelta(minutes=15)).strftime(stamp)},
            }
            return httpx.Response(200, json={"value": [item]})
        return httpx.Response(404)


def company(client: httpx.AsyncClient, scope: str = "") -> FakeCompany:
    fake = FakeCompany(scope)
    calendars = client.app_state.calendars  # type: ignore[attr-defined]
    calendars.http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return fake


async def sign_in(client: httpx.AsyncClient, provider: str) -> httpx.Response:
    started = await client.post(
        f"/calendars/oauth/{provider}/start", json={"origin": "http://127.0.0.1:8000"}
    )
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["url"]).query)
    assert query["redirect_uri"] == ["http://localhost:8000/calendars/oauth/callback"]
    return await client.get(
        f"/calendars/oauth/callback?state={query['state'][0]}&code=the-code",
        follow_redirects=False,
    )


async def mac_with_calendars(client: httpx.AsyncClient) -> dict[str, Any]:
    await client.post("/calendars/mac")
    now = datetime.now(UTC)
    report = {
        "access": "granted",
        "calendars": [
            {
                "id": "home",
                "name": "Home",
                "account": "iCloud",
                "writable": True,
                "color": "#3366cc",
            },
            {
                "id": "holidays",
                "name": "Holidays",
                "account": "Subscribed",
                "writable": False,
                "color": "#999999",
            },
        ],
        "events": [
            {
                "uid": "A@1",
                "calendar": "Home",
                "calendar_id": "home",
                "title": "Dentist",
                "starts_at": (now + timedelta(hours=1)).isoformat(),
                "ends_at": (now + timedelta(hours=2)).isoformat(),
            },
            {
                "uid": "H@1",
                "calendar": "Holidays",
                "calendar_id": "holidays",
                "title": "Bank holiday",
                "starts_at": (now + timedelta(hours=1)).isoformat(),
                "ends_at": (now + timedelta(hours=2)).isoformat(),
                "all_day": True,
            },
        ],
    }
    return (await client.post("/calendars/mac/helper", json=report)).json()


async def attached_document(client: httpx.AsyncClient, name: str, text_: str) -> str:
    source = (await client.post("/sources/upload", json={"name": name})).json()
    await client.post(
        f"/sources/{source['id']}/documents", json={"path": "brief.md", "text": text_}
    )
    return str(source["id"])


# ---- Signing in with a name and password ----

PASSWORD = "correct horse battery"


async def set_password(client: httpx.AsyncClient, **extra: str) -> httpx.Response:
    return await client.put("/settings/login", json={"name": "Alex", "password": PASSWORD, **extra})


async def password_sign_in(
    client: httpx.AsyncClient, name: str = "alex", password: str = PASSWORD, **extra: Any
) -> httpx.Response:
    return await client.post(
        "/session/password",
        json={"name": name, "password": password, **extra},
        headers=NO_BEARER,
    )


# ---- Tools in discussions ----


class _BoardTools:
    """A project's granted tools, as a discussion sees them."""

    def __init__(self) -> None:
        self.called: list[tuple[str, str, dict[str, Any]]] = []

    def offered(self) -> list[dict[str, Any]]:
        return [
            {
                "server": "Boards",
                "name": "board_list",
                "description": "List boards",
                "schema": {},
                "needs_approval": False,
                "price_usd": None,
            },
            {
                "server": "Boards",
                "name": "board_create",
                "description": "Create a board",
                "schema": {},
                "needs_approval": True,
                "price_usd": None,
            },
            {
                "server": "Boards",
                "name": "board_export",
                "description": "Paid export",
                "schema": {},
                "needs_approval": False,
                "price_usd": 0.5,
            },
        ]

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        self.called.append((server, tool, arguments))
        return "3 boards: Plan, Roadmap, Ideas"


# ---- A stuck task asks for help ----

STUCK = {
    "what": "click 'Case studies' on www.loot-drop.io",
    "error": "action_timed_out",
    "tries": 2,
    "url": "https://www.loot-drop.io/",
    "step": "s3",
    "brief": "Read the case studies",
}


async def stuck_run(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """A run paused on a stuck step, as the dispatcher leaves one."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Stuck {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Audit it"})
    ).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    repository.stop(UUID(run["id"]), "PAUSED")
    repository.add_event(UUID(run["id"]), "stuck_ask", STUCK)
    return str(run["id"])


# ---- What a build made, and how it is previewed ----


async def built_run(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """A run whose build made a small website, as the engineering step leaves it."""
    from app import builds

    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Build {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "A site"})
    ).json()
    settings = client.settings  # type: ignore[attr-defined]
    folder = builds.folder_for(settings, run["id"])
    builds.ensure_repository(folder)
    (folder / "index.html").write_text("<script src='app.js'></script><h1>Colour</h1>")
    (folder / "app.js").write_text("window.print;")
    repository = client.app_state.repository  # type: ignore[attr-defined]
    repository.merge_state(
        UUID(run["id"]),
        {
            "build_folder": run["id"],
            "engineering": {
                "status": "completed",
                "landed": True,
                "errors": [],
                "summary": "Made it.",
            },
        },
    )
    return str(run["id"])


async def versioned_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, str]:
    """A build with two versions: this task made the first."""
    from app import builds
    from tests.integration.test_build_versions import commit_version

    run_id = await built_run(client, monkeypatch)
    folder = builds.folder_for(client.settings, run_id)  # type: ignore[attr-defined]
    one = commit_version(folder, {"index.html": "<h1>One</h1>"}, run_id, "Build: one")
    two = commit_version(folder, {"index.html": "<h1>Two</h1>"}, str(uuid4()), "Build: two")
    client.app_state.repository.merge_state(UUID(run_id), {"build_commit": one})  # type: ignore[attr-defined]
    return run_id, one, two


async def _running_in_a_thread(client: httpx.AsyncClient) -> tuple[str, str]:
    """A discussion with work running in it: (topic id, run id)."""
    from app.db.models import Run as RunRow

    started = (await client.post("/topics", json={"text": "Pilot pricing"})).json()
    topic_id = started["topic"]["id"]
    project_id = (await client.post("/projects", json={"name": f"Q {uuid4().hex[:6]}"})).json()[
        "id"
    ]
    run_id = (
        await client.post("/runs", json={"project_id": project_id, "objective": "Three tiers"})
    ).json()["id"]
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        row = session.get(RunRow, UUID(run_id))
        assert row is not None
        row.status = "RUNNING"
        row.topic_id = UUID(topic_id)
    return topic_id, run_id
