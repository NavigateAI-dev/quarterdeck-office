"""Tasks: runs, approvals, review, limits, artifacts and their flow."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.db.models import Run as RunRow
from tests.integration.api_support import (
    ANSWER,
    AUTH,
    DASHBOARD,
    DOCUMENT_PAYLOAD,
    NO_BEARER,
    PROVIDER_KEY,
    FakeAdapter,
    FakeIndexer,
    RoutingJudge,
    _action_path,
    _delivered_in,
    _edited,
    _finished,
    _granted_filings,
    _proposed,
    _RelevanceJudge,
    _room_with_team,
    _running_in_a_thread,
    _said,
    _said_in,
    _set_run,
    _team_post,
    _topic_with,
    artifact_of,
    attached_document,
    budget_paused_run,
    building_run,
    built_run,
    calendar_link_server,
    calendar_soon,
    checked_connection,
    finished_run,
    granted_project,
    memory_run,
    new_agent,
    open_approval,
    project_with_work,
    stopped_run,
    stuck_run,
    venture_and_projects,
    versioned_run,
)

pytestmark = pytest.mark.integration


async def test_run_records_durable_intent_and_returns_immediately(
    client: httpx.AsyncClient,
) -> None:
    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    response = await client.post(
        "/runs", json={"project_id": project_id, "objective": "Assess demand"}
    )
    assert response.status_code == 202
    body = response.json()
    # A task-box request goes to the lead by default ...
    assert body["workflow_name"] == "lead"
    # ... and to the full team when the owner ticks it.
    team = await client.post(
        "/runs", json={"project_id": project_id, "objective": "Assess demand", "full_team": True}
    )
    assert team.json()["workflow_name"] == "v0_analysis"
    # The run row exists before the response, so the work survives a restart.
    fetched = await client.get(f"/runs/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["root_task_id"] == body["root_task_id"]


async def test_run_for_unknown_project_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/runs", json={"project_id": str(uuid4()), "objective": "Assess demand"}
    )
    assert response.status_code == 404


async def test_artifacts_are_listed_per_run(client: httpx.AsyncClient) -> None:
    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run_id = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()[
        "id"
    ]
    listed = await client.get(f"/runs/{run_id}/artifacts")
    assert listed.status_code == 200
    assert isinstance(listed.json(), list)
    assert (await client.get(f"/runs/{uuid4()}/artifacts")).status_code == 404


async def test_approvals_require_authentication(client: httpx.AsyncClient) -> None:
    approval_id, digest = await open_approval(client)
    body = {"scope_digest": digest}
    assert (
        await client.get("/approvals/pending", headers={"Authorization": ""})
    ).status_code == 401
    assert (
        await client.post(
            f"/approvals/{approval_id}/approve", json=body, headers={"Authorization": ""}
        )
    ).status_code == 401
    wrong = {"Authorization": "Bearer wrong-token-of-sufficient-length-here"}
    assert (
        await client.post(f"/approvals/{approval_id}/approve", json=body, headers=wrong)
    ).status_code == 401


async def test_pending_approval_exposes_the_brief_and_digest(client: httpx.AsyncClient) -> None:
    approval_id, digest = await open_approval(client)
    listed = (await client.get("/approvals/pending", headers=AUTH)).json()
    assert len(listed) == 1
    assert listed[0]["approval"]["id"] == approval_id
    assert listed[0]["brief"]["scope_digest"] == digest
    assert listed[0]["brief"]["proposed_engineering_scope"] == "Add an export endpoint"


async def test_a_stale_scope_digest_is_refused(client: httpx.AsyncClient) -> None:
    approval_id, _ = await open_approval(client)
    response = await client.post(
        f"/approvals/{approval_id}/approve", json={"scope_digest": "0" * 64}, headers=AUTH
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "stale_scope"


async def test_a_decision_cannot_be_applied_twice(client: httpx.AsyncClient) -> None:
    approval_id, digest = await open_approval(client)
    body = {"scope_digest": digest}
    first = await client.post(f"/approvals/{approval_id}/approve", json=body, headers=AUTH)
    assert first.status_code == 200
    second = await client.post(f"/approvals/{approval_id}/approve", json=body, headers=AUTH)
    assert second.status_code == 409
    assert second.json()["detail"] == "already_resolved"


async def test_conflicting_decisions_cannot_both_apply(client: httpx.AsyncClient) -> None:
    approval_id, digest = await open_approval(client)
    body = {"scope_digest": digest}
    assert (
        await client.post(f"/approvals/{approval_id}/reject", json=body, headers=AUTH)
    ).status_code == 200
    conflicting = await client.post(f"/approvals/{approval_id}/approve", json=body, headers=AUTH)
    assert conflicting.status_code == 409


async def test_approver_identity_is_derived_and_not_submitted(client: httpx.AsyncClient) -> None:
    approval_id, digest = await open_approval(client)
    forged = await client.post(
        f"/approvals/{approval_id}/approve",
        json={"scope_digest": digest, "resolved_by": "someone-else", "approved": True},
        headers=AUTH,
    )
    # A forged identity or approved flag is not a field the API accepts at all.
    assert forged.status_code == 422
    accepted = await client.post(
        f"/approvals/{approval_id}/approve", json={"scope_digest": digest}, headers=AUTH
    )
    assert accepted.json()["resolved_by"] == "owner"


async def test_approval_creates_exactly_one_decision_record(client: httpx.AsyncClient) -> None:
    from sqlalchemy import func, select

    from app.db.models import Decision

    approval_id, digest = await open_approval(client)
    await client.post(
        f"/approvals/{approval_id}/approve",
        json={"scope_digest": digest, "comment": "Proceed"},
        headers=AUTH,
    )
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        count = session.execute(select(func.count()).select_from(Decision)).scalar_one()
        record = session.scalars(select(Decision)).one()
    assert count == 1
    assert record.approved_by == "owner"
    assert record.decision == "Build a narrow prototype"


async def test_rejection_creates_no_decision_record(client: httpx.AsyncClient) -> None:
    from sqlalchemy import func, select

    from app.db.models import Decision

    approval_id, digest = await open_approval(client)
    await client.post(
        f"/approvals/{approval_id}/reject", json={"scope_digest": digest}, headers=AUTH
    )
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        count = session.execute(select(func.count()).select_from(Decision)).scalar_one()
    assert count == 0


async def test_revision_requires_a_valid_target(client: httpx.AsyncClient) -> None:
    approval_id, digest = await open_approval(client)
    bad = await client.post(
        f"/approvals/{approval_id}/revise",
        json={"scope_digest": digest, "revision_target": "engineering"},
        headers=AUTH,
    )
    assert bad.status_code == 422
    good = await client.post(
        f"/approvals/{approval_id}/revise",
        json={"scope_digest": digest, "revision_target": "strategy"},
        headers=AUTH,
    )
    assert good.status_code == 200
    assert good.json()["revision_target"] == "strategy"


async def test_runs_can_be_listed_for_a_project(client: httpx.AsyncClient) -> None:
    first = (await client.post("/projects", json={"name": "One"})).json()["id"]
    second = (await client.post("/projects", json={"name": "Two"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": first, "objective": "o"})).json()
    listed = (await client.get(f"/runs?project_id={first}")).json()
    assert [row["id"] for row in listed] == [run["id"]]
    assert (await client.get(f"/runs?project_id={second}")).json() == []
    assert len((await client.get("/runs")).json()) == 1


async def test_a_run_reports_its_objective_before_it_finishes(
    client: httpx.AsyncClient,
) -> None:
    """The state projection is only written at the end, so read the task."""
    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (
        await client.post("/runs", json={"project_id": project_id, "objective": "Assess demand"})
    ).json()
    detail = (await client.get(f"/runs/{run['id']}")).json()
    assert detail["objective"] == "Assess demand"


async def test_artifact_content_is_readable(client: httpx.AsyncClient) -> None:
    from app.artifacts import ArtifactMetadata

    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()
    store = client._transport.app.state.repository.store  # type: ignore[attr-defined]
    metadata = ArtifactMetadata(project_id=UUID(project_id), type="synthesis", created_by="chief")
    store.write(metadata, '{"recommendation": "build it"}')
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    from app.db.models import Artifact

    with factory() as session, session.begin():
        session.add(
            Artifact(
                id=metadata.artifact_id,
                project_id=UUID(project_id),
                run_id=UUID(run["id"]),
                type="synthesis",
                path=str(store.path_of(metadata)),
                created_by="chief",
                version=1,
            )
        )
    body = (await client.get(f"/artifacts/{metadata.artifact_id}")).json()
    assert body["content"] == '{"recommendation": "build it"}'
    assert body["type"] == "synthesis"
    assert (await client.get(f"/artifacts/{uuid4()}")).status_code == 404


async def test_a_stopped_run_can_be_sent_back_by_a_person(client: httpx.AsyncClient) -> None:
    """A run that stops for human attention must be answerable."""
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={
            "action": "revise",
            "comment": "Narrow it to EU vendors",
            "revision_target": "strategy",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    # Queued for the worker to claim, not already running.
    assert response.json()["status"] == "PENDING"


async def test_a_stopped_run_can_be_closed(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={"action": "close", "comment": "Not worth pursuing"},
    )
    assert response.json()["status"] == "CLOSED"


async def test_review_and_approval_both_require_the_owner_token(
    client: httpx.AsyncClient,
) -> None:
    """Every data route, including review and approval, is behind the owner boundary."""
    run_id = await stopped_run(client)
    no_token = {"Authorization": ""}
    review = await client.post(
        f"/runs/{run_id}/review", json={"action": "close", "comment": "x"}, headers=no_token
    )
    assert review.status_code == 401

    approval_id, digest = await open_approval(client)
    unauthenticated = await client.post(
        f"/approvals/{approval_id}/approve", json={"scope_digest": digest}, headers=no_token
    )
    assert unauthenticated.status_code == 401


async def test_a_running_run_cannot_be_reviewed(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client, status="RUNNING")
    response = await client.post(f"/runs/{run_id}/review", json={"action": "close", "comment": "x"})
    assert response.status_code == 409
    assert response.json()["detail"] == "run_not_awaiting_review"


async def test_a_review_needs_an_instruction(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client)
    response = await client.post(f"/runs/{run_id}/review", json={"action": "revise", "comment": ""})
    assert response.status_code == 422


async def test_an_unknown_revision_target_is_refused(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={"action": "revise", "comment": "x", "revision_target": "engineering"},
    )
    assert response.status_code == 422


async def test_a_reviewed_run_can_actually_be_claimed(client: httpx.AsyncClient) -> None:
    """The worker claims out of PENDING, so a reviewed run must return to it."""
    from app.orchestrator.runner import RunRepository

    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={"action": "revise", "comment": "Narrow the scope", "revision_target": "strategy"},
    )
    assert response.json()["status"] == "PENDING"
    repository: RunRepository = client._transport.app.state.repository  # type: ignore[attr-defined]
    assert repository.claim(UUID(run_id)) is True


async def test_a_reviewed_run_starts_a_new_bounded_cycle(client: httpx.AsyncClient) -> None:
    from app.db.models import Run

    run_id = await stopped_run(client)
    await client.post(
        f"/runs/{run_id}/review",
        json={"action": "revise", "comment": "Focus on cost", "revision_target": "research"},
    )
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        run = session.get(Run, UUID(run_id))
    assert run is not None
    assert run.state_json["human_cycles"] == 1
    assert run.state_json["revision_count"] == 0
    assert run.state_json["critique"]["required_corrections"] == ["Focus on cost"]
    assert run.state_json["critique"]["revision_target"] == "research"


async def test_accepting_a_task_stopped_before_research_runs_the_research(
    client: httpx.AsyncClient,
) -> None:
    """E1: a human PASS routes to the summary; with nothing researched, that
    wrote an answer from the plan alone."""
    from app.db.models import Run

    run_id = await stopped_run(client, researched=False)
    await client.post(
        f"/runs/{run_id}/review",
        json={"action": "accept", "comment": "The six industries are retail, travel and food."},
    )
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        run = session.get(Run, UUID(run_id))
    assert run is not None
    critique = run.state_json["critique"]
    assert critique["verdict"] == "REVISE" and critique["revision_target"] == "research"
    assert critique["required_corrections"] == ["The six industries are retail, travel and food."]


async def test_accept_is_offered_and_records_the_decision(
    client: httpx.AsyncClient,
) -> None:
    from sqlalchemy import select

    from app.db.models import Artifact

    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={"action": "accept", "comment": "The limits are stated; proceed"},
    )
    assert response.status_code == 200
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        kinds = list(session.scalars(select(Artifact.type).where(Artifact.run_id == UUID(run_id))))
    assert "human_review" in kinds


async def test_a_closed_run_records_the_reason(client: httpx.AsyncClient) -> None:
    from sqlalchemy import select

    from app.db.models import Artifact

    run_id = await stopped_run(client)
    await client.post(f"/runs/{run_id}/review", json={"action": "close", "comment": "Superseded"})
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        stored = session.scalars(select(Artifact).where(Artifact.run_id == UUID(run_id))).all()
    assert any(a.type == "human_review" and a.created_by == "owner" for a in stored)


async def test_an_unknown_review_action_is_refused(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review", json={"action": "approve_engineering", "comment": "x"}
    )
    assert response.status_code == 422


async def test_a_run_reports_its_approval_status(client: httpx.AsyncClient) -> None:
    """Progress must be readable without the owner token."""
    approval_id, digest = await open_approval(client)
    approval = (await client.get("/approvals/pending", headers=AUTH)).json()[0]
    run_id = approval["approval"]["run_id"]

    pending = (await client.get(f"/runs/{run_id}")).json()
    assert pending["approval_status"] == "PENDING"

    await client.post(
        f"/approvals/{approval_id}/approve", json={"scope_digest": digest}, headers=AUTH
    )
    resolved = (await client.get(f"/runs/{run_id}")).json()
    assert resolved["approval_status"] == "APPROVED"


async def test_a_run_without_an_approval_reports_none(client: httpx.AsyncClient) -> None:
    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()
    assert (await client.get(f"/runs/{run['id']}")).json()["approval_status"] is None


async def test_approving_actually_resumes_the_run(client: httpx.AsyncClient) -> None:
    """Every resume path must requeue: a claim only succeeds out of PENDING."""
    from app.db.models import Run

    approval_id, digest = await open_approval(client)
    approval = (await client.get("/approvals/pending", headers=AUTH)).json()[0]
    run_id = UUID(approval["approval"]["run_id"])
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == run_id).values(status="WAITING_FOR_APPROVAL"))

    await client.post(
        f"/approvals/{approval_id}/approve", json={"scope_digest": digest}, headers=AUTH
    )
    with factory() as session:
        run = session.get(Run, run_id)
    assert run is not None
    # Either queued or already picked up, but never left waiting.
    assert run.status != "WAITING_FOR_APPROVAL"


async def test_a_waiting_run_can_be_requeued(client: httpx.AsyncClient) -> None:
    from app.db.models import Run
    from app.orchestrator.runner import RunRepository

    project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()
    run_id = UUID(run["id"])
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == run_id).values(status="WAITING_FOR_APPROVAL"))
    repository: RunRepository = client._transport.app.state.repository  # type: ignore[attr-defined]
    assert repository.requeue(run_id) is True
    assert repository.claim(run_id) is True


async def test_an_archived_agent_is_not_snapshotted(client: httpx.AsyncClient) -> None:
    agent = await new_agent(client)
    archived = await client.post(f"/agents/{agent['id']}/archive")
    assert archived.json()["status"] == "ARCHIVED"
    project = (await client.post("/projects", json={"name": "Archive"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Assess"})
    ).json()
    snapshot = (await client.get(f"/runs/{run['id']}")).json()["config_snapshot"]
    assert snapshot["agents"] == []


async def test_the_worker_runs_a_snapshotted_agent_on_its_own_connection(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A03: the run uses the enabled version's model and key, not the default alias."""
    from app.gateway.client import GatewayError
    from app.gateway.direct import DirectCompleter

    connection_id = await checked_connection(client, monkeypatch)
    await new_agent(client, connection_id=connection_id)
    project = (await client.post("/projects", json={"name": "Routing"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Assess"})
    ).json()
    [entry] = (await client.get(f"/runs/{run['id']}")).json()["config_snapshot"]["agents"]

    worker = client.app_state.worker  # type: ignore[attr-defined]
    completer = worker._completer_for(entry)
    assert isinstance(completer, DirectCompleter)
    assert completer.model_id == "gpt-5"
    assert completer.credential == PROVIDER_KEY

    await client.delete(f"/connections/{connection_id}/credential")
    unavailable = worker._completer_for(entry)
    with pytest.raises(GatewayError, match="credential_unavailable"):
        await unavailable.complete("x", [{"role": "user", "content": "x"}])


async def test_a_run_request_uses_the_layered_context(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = client.app_state.worker  # type: ignore[attr-defined]
    submitted: list[Any] = []
    monkeypatch.setattr(worker, "submit", submitted.append)
    project = (await client.post("/projects", json={"name": "Layered"})).json()
    await client.post("/runs", json={"project_id": project["id"], "objective": "Assess"})
    [job] = submitted
    assert job.context["organization"] == worker.settings.office_name
    assert job.context["decisions"] == []
    assert job.context["prior_outcomes"] == []
    assert job.context["local_sources"] == []
    from datetime import UTC, datetime

    assert job.context["as_of"] == datetime.now(UTC).date().isoformat()


async def test_a_revision_without_a_target_is_routed_by_judgment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rework and revision targets are judged in the shared actions.
    monkeypatch.setattr("app.run_actions.build_judge", lambda settings: RoutingJudge())
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={"action": "revise", "comment": "Narrow the ideal customer profile"},
    )
    assert response.status_code == 200
    artifacts = (await client.get(f"/runs/{run_id}/artifacts")).json()
    review = next(a for a in artifacts if a["type"] == "human_review")
    content = json.loads((await client.get(f"/artifacts/{review['id']}")).json()["content"])
    assert "[revision target judged (0.90)]" in content["comment"]


async def test_an_ambiguous_revision_note_asks_the_owner_to_choose(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.run_actions.build_judge", lambda settings: RoutingJudge(("unclear", 0.7))
    )
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review", json={"action": "revise", "comment": "Make it better"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "revision_target_ambiguous"


async def test_an_explicit_target_needs_no_judgment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(settings: Any) -> Any:
        raise AssertionError("judge must not be built")

    monkeypatch.setattr("app.api.routers.runs.build_judge", fail)
    run_id = await stopped_run(client)
    response = await client.post(
        f"/runs/{run_id}/review",
        json={"action": "revise", "comment": "x", "revision_target": "research"},
    )
    assert response.status_code == 200


async def test_intake_proposes_a_task_in_the_named_project_without_starting_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = (await client.post("/projects", json={"name": "Market intel"})).json()
    judge = RoutingJudge()
    judge.route_answer = {"action": ("start_task", 0.93), "project": ("Market intel", 0.88)}
    monkeypatch.setattr("app.api.routers.runs.build_judge", lambda settings: judge)
    text = "Prepare this week's competitor pricing brief for market intel"
    body = (await client.post("/intake", json={"text": text})).json()
    assert body["action"] == "start_task"
    assert body["proposal"] == {"project_id": project["id"], "objective": text}
    assert body["executed"] is False
    assert (await client.get("/runs")).json() == []


async def test_intake_starts_the_task_only_when_told_to(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.post("/projects", json={"name": "Market intel"})
    judge = RoutingJudge()
    judge.route_answer = {"action": ("start_task", 0.93), "project": ("Market intel", 0.88)}
    monkeypatch.setattr("app.api.routers.runs.build_judge", lambda settings: judge)
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    body = (await client.post("/intake", json={"text": "Brief on pricing", "execute": True})).json()
    assert body["executed"] is True
    assert len((await client.get("/runs")).json()) == 1


async def test_intake_answers_status_requests_and_refuses_uncertain_ones(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = RoutingJudge()
    monkeypatch.setattr("app.api.routers.runs.build_judge", lambda settings: judge)
    judge.route_answer = {"action": ("office_status", 0.9), "project": ("none", 0.9)}
    status = (await client.post("/intake", json={"text": "is everything ok?"})).json()
    assert status["action"] == "office_status"
    assert "database" in status["result"]
    judge.route_answer = {"action": ("start_task", 0.4), "project": ("none", 0.5)}
    unsure = (await client.post("/intake", json={"text": "hmm", "execute": True})).json()
    assert unsure["action"] == "clarify"
    assert unsure["executed"] is False


async def test_intake_without_typesafe_is_unavailable(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.routers.runs.build_judge", lambda settings: None)
    response = await client.post("/intake", json={"text": "status"})
    assert response.status_code == 503


async def test_run_context_names_the_granted_local_sources(
    client: httpx.AsyncClient, indexer: FakeIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = client.app_state.worker  # type: ignore[attr-defined]
    submitted: list[Any] = []
    monkeypatch.setattr(worker, "submit", submitted.append)
    project, _ = await granted_project(client, indexer, "Ctx", "Some text.")
    await client.post("/runs", json={"project_id": project, "objective": "Assess"})
    assert submitted[0].context["local_sources"] == ["Ctx (1 documents)"]


async def test_an_artifact_downloads_as_a_file(client: httpx.AsyncClient) -> None:
    project, dispatch = await project_with_work(client, "Downloadable")
    artifacts = (await client.get(f"/runs/{dispatch.run_id}/artifacts")).json()
    response = await client.get(f"/artifacts/{artifacts[0]['id']}/download")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "Downloadable rec" in response.text


async def test_a_run_records_its_budget(client: httpx.AsyncClient) -> None:
    project = (await client.post("/projects", json={"name": "Budgeted"})).json()
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": "Assess",
                "budget": {"max_tokens": 50_000, "max_cost_usd": 0.5, "max_minutes": 10},
            },
        )
    ).json()
    usage = (await client.get(f"/runs/{run['id']}/usage")).json()
    assert usage["budget"] == {"max_tokens": 50_000, "max_cost_usd": 0.5, "max_minutes": 10}
    assert usage["spent"]["tokens"] == 0


async def test_a_run_without_a_budget_gets_the_default(client: httpx.AsyncClient) -> None:
    project = (await client.post("/projects", json={"name": "Default budget"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "x"})).json()
    budget = (await client.get(f"/runs/{run['id']}/usage")).json()["budget"]
    assert budget["max_tokens"] > 0


async def test_usage_is_persisted_and_totalled_by_role(client: httpx.AsyncClient) -> None:
    from app.events import InvocationEvent

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Metered"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Objective")
    for role, cost in (("researcher", 0.02), ("researcher", None), ("critic", 0.01)):
        state.repository.record_usage(
            InvocationEvent(
                run_id=dispatch.run_id,
                task_id=dispatch.task_id,
                role=role,
                model_alias="0002",
                project_id=UUID(project["id"]),
                status="ok",
                prompt_tokens=100,
                completion_tokens=20,
                estimated_cost=cost,
            )
        )
    usage = (await client.get(f"/runs/{dispatch.run_id}/usage")).json()
    assert usage["spent"]["tokens"] == 360
    assert usage["spent"]["cost_usd"] == pytest.approx(0.03)
    assert usage["spent"]["cost_reported"] is False  # one call reported no cost
    roles = {row["role"]: row for row in usage["by_role"]}
    assert roles["researcher"]["calls"] == 2
    assert state.repository.spent_tokens(dispatch.run_id) == 360


async def test_a_run_from_before_budgets_reports_the_budget_it_runs_under(
    client: httpx.AsyncClient,
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Legacy"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Objective")  # no budget stored
    usage = (await client.get(f"/runs/{dispatch.run_id}/usage")).json()
    assert usage["budget"] == state.worker.settings.default_budget


async def test_owner_controls_are_recorded_and_listed(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client, status="RUNNING")
    posted = await client.post(
        f"/runs/{run_id}/controls", json={"kind": "instruction", "text": "Focus on Europe"}
    )
    assert posted.status_code == 201
    assert posted.json()["status"] == "QUEUED"
    listed = (await client.get(f"/runs/{run_id}/controls")).json()
    assert [(c["kind"], c["text"], c["status"]) for c in listed] == [
        ("instruction", "Focus on Europe", "QUEUED")
    ]


async def test_an_instruction_needs_text(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client, status="RUNNING")
    response = await client.post(f"/runs/{run_id}/controls", json={"kind": "instruction"})
    assert response.status_code == 422


async def test_pausing_a_queued_run_holds_it_and_resume_requeues_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = client.app_state.worker  # type: ignore[attr-defined]
    submitted: list[Any] = []
    monkeypatch.setattr(worker, "submit", submitted.append)
    project = (await client.post("/projects", json={"name": "Pausable"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "x"})).json()
    submitted.clear()
    paused = (await client.post(f"/runs/{run['id']}/controls", json={"kind": "pause"})).json()
    assert paused["status"] == "APPLIED"
    assert (await client.get(f"/runs/{run['id']}")).json()["status"] == "PAUSED"
    resumed = await client.post(f"/runs/{run['id']}/resume")
    assert resumed.status_code == 202
    assert (await client.get(f"/runs/{run['id']}")).json()["status"] == "PENDING"
    assert len(submitted) == 1


async def test_resuming_a_run_that_is_not_paused_is_refused(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client, status="RUNNING")
    response = await client.post(f"/runs/{run_id}/resume")
    assert response.status_code == 409
    assert response.json()["detail"] == "run_not_paused"


async def test_cancelling_during_an_approval_wait_supersedes_the_approval(
    client: httpx.AsyncClient,
) -> None:
    approval_id, digest = await open_approval(client)
    pending = (await client.get("/approvals/pending")).json()
    run_id = next(p["approval"]["run_id"] for p in pending if p["approval"]["id"] == approval_id)
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        session.execute(
            update(RunRow).where(RunRow.id == UUID(run_id)).values(status="WAITING_FOR_APPROVAL")
        )
    cancelled = await client.post(f"/runs/{run_id}/controls", json={"kind": "cancel"})
    assert cancelled.json()["status"] == "APPLIED"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "CANCELLED"
    decide = await client.post(f"/approvals/{approval_id}/approve", json={"scope_digest": digest})
    assert decide.status_code == 409


async def test_events_can_be_read_after_a_cursor(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client, status="RUNNING")
    await client.post(f"/runs/{run_id}/controls", json={"kind": "instruction", "text": "a"})
    await client.post(f"/runs/{run_id}/controls", json={"kind": "instruction", "text": "b"})
    events = (await client.get(f"/runs/{run_id}/events")).json()
    assert [e["kind"] for e in events] == ["control_queued", "control_queued"]
    after = (
        await client.get(f"/runs/{run_id}/events", params={"after": events[0]["cursor"]})
    ).json()
    assert [e["payload"]["text"] for e in after] == ["b"]


async def test_a_pinned_skill_version_is_captured_in_the_run_snapshot(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/skills/install", json={"skill_id": "comparison-table", "version": "1.0.0"})
    agent = await new_agent(
        client,
        name="Writer",
        role="summarist",
        skills=[{"skill_id": "comparison-table", "version": "1.0.0"}],
    )
    assert agent["versions"][0]["skills"] == [{"skill_id": "comparison-table", "version": "1.0.0"}]
    project = (await client.post("/projects", json={"name": "Skilled"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "x"})).json()
    [entry] = (await client.get(f"/runs/{run['id']}")).json()["config_snapshot"]["agents"]
    [skill] = entry["skills"]
    assert (skill["skill_id"], skill["version"]) == ("comparison-table", "1.0.0")
    assert "comparison table first" in skill["instructions"]


async def test_a_run_can_be_saved_and_reused_as_a_template(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    project = (await client.post("/projects", json={"name": "Weekly intel"})).json()
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": "Weekly competitor pricing brief",
                "budget": {"max_tokens": 200_000, "max_minutes": 15},
            },
        )
    ).json()
    template = (await client.post(f"/runs/{run['id']}/template", json={"name": "Pricing"})).json()
    assert template["objective"] == "Weekly competitor pricing brief"
    assert template["budget"]["max_tokens"] == 200_000
    started = await client.post(f"/templates/{template['id']}/run")
    assert started.status_code == 202
    usage = (await client.get(f"/runs/{started.json()['id']}/usage")).json()
    assert usage["budget"]["max_tokens"] == 200_000


async def test_the_owner_renames_a_task_and_can_go_back_to_its_own_name(
    client: httpx.AsyncClient,
) -> None:
    """10 Oct (owner, on a phone): a task's title could not be changed at all."""
    project = (await client.post("/projects", json={"name": "Renames"})).json()
    run = (
        await client.post(
            "/runs",
            json={"project_id": project["id"], "objective": "Compare three CRMs for a small team"},
        )
    ).json()
    renamed = await client.put(f"/runs/{run['id']}/title", json={"title": "  CRM shortlist  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "CRM shortlist"
    assert (await client.get(f"/runs/{run['id']}")).json()["title"] == "CRM shortlist"
    back = await client.put(f"/runs/{run['id']}/title", json={"title": ""})
    assert back.json()["title"] != "" and "CRM" in back.json()["title"], "derived again"
    missing = await client.put(f"/runs/{uuid4()}/title", json={"title": "x"})
    assert missing.status_code == 404


async def test_office_settings_round_trip_and_apply_to_new_work(client: httpx.AsyncClient) -> None:
    before = (await client.get("/settings")).json()
    assert before["settings"]["run_max_tokens"] == before["configured"]["run_max_tokens"]
    saved = (
        await client.put(
            "/settings",
            json={"run_max_tokens": 12345, "run_max_minutes": 7, "monthly_cost_limit_usd": 25.0},
        )
    ).json()
    assert saved["settings"]["run_max_tokens"] == 12345
    project = (await client.post("/projects", json={"name": "Budgeted"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    usage = (await client.get(f"/runs/{run['id']}/usage")).json()
    assert usage["budget"]["max_tokens"] == 12345
    assert usage["budget"]["max_minutes"] == 7
    # Clearing returns to the configured value.
    cleared = (await client.put("/settings", json={"run_max_tokens": None})).json()
    assert cleared["settings"]["run_max_tokens"] == cleared["configured"]["run_max_tokens"]


async def test_the_monthly_limit_refuses_new_work_but_keeps_the_office_readable(
    client: httpx.AsyncClient,
) -> None:
    from app.db.models import UsageEvent

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Capped"})).json()
    first = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "o"})
    ).json()
    with state.session_factory() as session, session.begin():
        session.add(
            UsageEvent(
                run_id=UUID(first["id"]),
                project_id=UUID(project["id"]),
                role="researcher",
                provider="openai",
                model_alias="0002",
                status="OK",
                prompt_tokens=1000,
                completion_tokens=500,
                estimated_cost=9.0,
                attempts=1,
                latency_seconds=1.5,
                occurred_at=datetime.now(UTC),
            )
        )
    await client.put("/settings", json={"monthly_cost_limit_usd": 5.0})
    refused = await client.post("/runs", json={"project_id": project["id"], "objective": "next"})
    assert refused.status_code == 409 and refused.json()["detail"] == "monthly_limit_reached"
    assert (await client.get("/office/overview")).status_code == 200
    settings = (await client.get("/settings")).json()
    assert settings["spent_this_month_usd"] >= 9.0
    await client.put("/settings", json={"monthly_cost_limit_usd": None})
    assert (
        await client.post("/runs", json={"project_id": project["id"], "objective": "after"})
    ).status_code == 202


async def test_the_usage_summary_totals_runs_and_chat(client: httpx.AsyncClient) -> None:
    from app.db.models import UsageEvent

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Measured"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    with state.session_factory() as session, session.begin():
        for role, tokens, cost, status, latency in [
            ("researcher", 100, 0.5, "ok", 2.0),
            ("researcher", 200, 0.25, "ok", 4.0),
            ("critic", 50, None, "error", 1.0),
        ]:
            session.add(
                UsageEvent(
                    run_id=UUID(run["id"]),
                    project_id=UUID(project["id"]),
                    role=role,
                    provider="openai",
                    model_alias="0002",
                    resolved_model="gpt-5",
                    status=status,
                    prompt_tokens=tokens,
                    completion_tokens=0,
                    estimated_cost=cost,
                    attempts=1,
                    latency_seconds=latency,
                    occurred_at=datetime.now(UTC),
                )
            )
    summary = (await client.get("/usage/summary?days=7")).json()
    assert summary["totals"]["tokens"] == 350
    assert summary["totals"]["cost_complete"] is False  # one call had no price
    assert {row["key"]: row["tokens"] for row in summary["by_role"]} == {
        "researcher": 300,
        "critic": 50,
    }
    assert summary["by_model"][0]["key"] == "gpt-5"
    assert summary["performance"]["failures"] == 1
    assert summary["performance"]["avg_seconds"] > 0


async def test_direction_aimed_at_one_step_waits_for_that_step(
    client: httpx.AsyncClient,
) -> None:
    """A note for the critic must not be handed to the researcher."""
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Aimed"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    for body in (
        {"kind": "instruction", "text": "Focus on Europe"},
        {"kind": "instruction", "text": "Check the pricing claim", "target_node": "critique"},
    ):
        assert (await client.post(f"/runs/{run['id']}/controls", json=body)).status_code == 201
    for_research = state.repository.receive_instructions(UUID(run["id"]), "research")
    assert for_research == ["Focus on Europe"]
    for_critique = state.repository.receive_instructions(UUID(run["id"]), "critique")
    assert for_critique == ["Focus on Europe", "Check the pricing claim"]
    controls = (await client.get(f"/runs/{run['id']}/controls")).json()
    aimed = next(c for c in controls if c["target_node"] == "critique")
    assert aimed["status"] == "RECEIVED" and aimed["received_node"] == "critique"


async def test_an_unknown_target_step_is_refused(client: httpx.AsyncClient) -> None:
    project = (await client.post("/projects", json={"name": "Aimed badly"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    response = await client.post(
        f"/runs/{run['id']}/controls",
        json={"kind": "instruction", "text": "x", "target_node": "accounting"},
    )
    assert response.status_code == 422


async def test_labels_are_kept_tidy_and_shown_on_the_board(client: httpx.AsyncClient) -> None:
    project = (await client.post("/projects", json={"name": "Labelled"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    updated = (
        await client.put(
            f"/runs/{run['id']}/labels",
            json={"labels": ["  pricing   research ", "pricing research", "Q4", ""]},
        )
    ).json()
    assert updated["labels"] == ["pricing research", "Q4"]  # tidied, no duplicates or blanks
    board = (await client.get("/office/board")).json()
    card = next(
        c for column in board["columns"] for c in column["cards"] if c["run_id"] == run["id"]
    )
    assert card["labels"] == ["pricing research", "Q4"]
    cleared = (await client.put(f"/runs/{run['id']}/labels", json={"labels": []})).json()
    assert cleared["labels"] == []


async def test_a_project_runs_under_its_own_limits_and_models(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import UsageEvent
    from app.project_settings import role_models

    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "app.api.routers.reach.adapter_for", lambda p, base_url=None: FakeAdapter(["m"])
    )
    connection = (
        await client.post(
            "/connections",
            json={"provider": "groq", "display_name": "Project model", "model_id": "m"},
        )
    ).json()
    project = (await client.post("/projects", json={"name": "Client A"})).json()
    other = (await client.post("/projects", json={"name": "Client B"})).json()
    saved = (
        await client.put(
            f"/projects/{project['id']}/settings",
            json={
                "monthly_cost_limit_usd": 5.0,
                "run_max_tokens": 4321,
                "role_models": {"researcher": connection["id"]},
            },
        )
    ).json()
    assert saved["project"]["run_max_tokens"] == 4321
    assert saved["in_force"]["max_tokens"] == 4321
    # A new task in that project uses its budget; another project is unaffected.
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    assert (await client.get(f"/runs/{run['id']}/usage")).json()["budget"]["max_tokens"] == 4321
    elsewhere = (
        await client.post("/runs", json={"project_id": other["id"], "objective": "o"})
    ).json()
    assert (await client.get(f"/runs/{elsewhere['id']}/usage")).json()["budget"][
        "max_tokens"
    ] != 4321
    with state.session_factory() as session:
        entries = role_models(session, state.worker.settings, UUID(project["id"]))
        assert entries["researcher"]["connection_id"] == connection["id"]
        assert role_models(session, state.worker.settings, UUID(other["id"])) == {}
    # Its own monthly limit stops its work, and only its work.
    with state.session_factory() as session, session.begin():
        session.add(
            UsageEvent(
                run_id=UUID(run["id"]),
                project_id=UUID(project["id"]),
                role="researcher",
                provider="groq",
                model_alias="m",
                status="ok",
                prompt_tokens=10,
                completion_tokens=10,
                estimated_cost=6.0,
                attempts=1,
                latency_seconds=1.0,
                occurred_at=datetime.now(UTC),
            )
        )
    refused = await client.post("/runs", json={"project_id": project["id"], "objective": "another"})
    assert refused.status_code == 409 and refused.json()["detail"] == "monthly_limit_reached"
    assert (
        await client.post("/runs", json={"project_id": other["id"], "objective": "fine"})
    ).status_code == 202
    summary = (await client.get(f"/projects/{project['id']}/summary")).json()
    assert summary["spent_this_month_usd"] == 6.0 and summary["tokens"] == 20
    # Clearing returns the project to the office's settings.
    cleared = (
        await client.put(
            f"/projects/{project['id']}/settings",
            json={"monthly_cost_limit_usd": None, "run_max_tokens": None, "role_models": {}},
        )
    ).json()
    assert cleared["project"]["monthly_cost_limit_usd"] is None
    assert cleared["in_force"]["max_tokens"] != 4321


async def test_a_note_can_be_reworded_or_withdrawn_until_an_agent_takes_it(
    client: httpx.AsyncClient,
) -> None:
    """Direction is the owner's until an agent has it."""
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Queued notes"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "o"})).json()
    first = (
        await client.post(
            f"/runs/{run['id']}/controls",
            json={"kind": "instruction", "text": "Look at Europe"},
        )
    ).json()
    reworded = (
        await client.put(
            f"/runs/{run['id']}/controls/{first['id']}",
            json={"text": "Look at Europe and the UK"},
        )
    ).json()
    assert reworded["text"] == "Look at Europe and the UK"
    second = (
        await client.post(
            f"/runs/{run['id']}/controls", json={"kind": "instruction", "text": "Ignore this"}
        )
    ).json()
    assert (await client.delete(f"/runs/{run['id']}/controls/{second['id']}")).status_code == 204
    remaining = (await client.get(f"/runs/{run['id']}/controls")).json()
    assert [row["text"] for row in remaining] == ["Look at Europe and the UK"]
    # Once an agent has taken it, it is no longer the owner's to change.
    state.repository.receive_instructions(UUID(run["id"]), "research")
    taken = await client.put(f"/runs/{run['id']}/controls/{first['id']}", json={"text": "too late"})
    assert taken.status_code == 409 and taken.json()["detail"] == "already_received"
    assert (await client.delete(f"/runs/{run['id']}/controls/{first['id']}")).status_code == 409
    # A withdrawn note never reaches an agent.
    assert "Ignore this" not in state.repository.receive_instructions(UUID(run["id"]), "strategy")


async def test_pausing_the_office_stops_new_work_but_not_work_in_flight(
    client: httpx.AsyncClient,
) -> None:
    """A switch the owner can reach, and what it does and does not stop."""
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Paused office"})).json()
    running = state.repository.create(UUID(project["id"]), "already going")
    assert state.repository.claim(running.run_id, "worker-one")
    paused = (await client.put("/settings", json={"pause_new_tasks": True})).json()
    assert paused["settings"]["pause_new_tasks"] is True
    refused = await client.post(
        "/runs", json={"project_id": project["id"], "objective": "something new"}
    )
    assert refused.status_code == 409 and refused.json()["detail"] == "office_paused"
    # What was already running is untouched, and still readable.
    assert (await client.get(f"/runs/{running.run_id}")).json()["status"] == "RUNNING"
    assert (await client.get("/office/overview")).status_code == 200
    await client.put("/settings", json={"pause_new_tasks": False})
    assert (
        await client.post("/runs", json={"project_id": project["id"], "objective": "after"})
    ).status_code == 202


async def test_a_paused_run_reports_the_ceiling_it_is_waiting_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    asked = (await client.get(f"/runs/{run_id}/question")).json()
    assert asked["kind"] == "budget"
    assert asked["limit"] == "tokens"
    assert asked["budget"]["max_tokens"] == 400000
    assert asked["spent"]["tokens"] == 397284


async def test_raising_the_ceiling_resumes_the_run_and_closes_the_question(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    response = await client.post(f"/runs/{run_id}/budget", json={"max_tokens": 600000})
    assert response.status_code == 202
    assert response.json()["budget"]["max_tokens"] == 600000
    assert response.json()["resumed"] is True
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "PENDING"
    # Answered: the drawer stops offering the choice.
    assert (await client.get(f"/runs/{run_id}/question")).json() is None


async def test_a_ceiling_below_what_is_already_spent_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the run stops again on its next call and the office looks broken."""
    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    response = await client.post(f"/runs/{run_id}/budget", json={"max_tokens": 397000})
    assert response.status_code == 409
    assert response.json()["detail"] == "below_spent_tokens"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "PAUSED"


async def test_raising_the_ceiling_of_a_running_run_does_not_requeue_it(
    client: httpx.AsyncClient,
) -> None:
    """Limits are editable while a task works; only a paused one is picked up."""
    run_id = await stopped_run(client, status="RUNNING")
    response = await client.post(f"/runs/{run_id}/budget", json={"max_tokens": 600000})
    assert response.status_code == 202
    assert response.json()["resumed"] is False
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "RUNNING"


async def test_a_raise_that_raises_nothing_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    response = await client.post(f"/runs/{run_id}/budget", json={})
    assert response.status_code == 422
    assert response.json()["detail"] == "nothing_raised"


async def test_resuming_a_run_held_at_its_ceiling_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the owner hit: Resume said "resumed" and nothing changed.

    The run restarted, refused its first call, and paused again in
    milliseconds. Offering a resume that cannot work is the bug.
    """
    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    response = await client.post(f"/runs/{run_id}/resume")
    assert response.status_code == 409
    assert response.json()["detail"] == "raise_the_limit_first"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "PAUSED"

    # Raising the ceiling is what makes it resumable.
    raised = await client.post(f"/runs/{run_id}/budget", json={"max_tokens": 600000})
    assert raised.status_code == 202
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "PENDING"


async def test_a_run_paused_at_a_time_ceiling_can_simply_resume(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minutes are counted per worker job, so a resume starts that clock again."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Clock {uuid4().hex[:6]}"})).json()
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": "Assess demand",
                "budget": {"max_tokens": 400000, "max_minutes": 5},
            },
        )
    ).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    repository.stop(UUID(run["id"]), "PAUSED")
    repository.add_event(
        UUID(run["id"]),
        "budget_ask",
        {"limit": "time", "node": "critic", "budget": {"max_minutes": 5}, "spent": {"tokens": 10}},
    )
    response = await client.post(f"/runs/{run['id']}/resume")
    assert response.status_code == 202
    assert (await client.get(f"/runs/{run['id']}")).json()["status"] == "PENDING"


async def test_an_ordinary_pause_still_resumes_without_a_raise(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a binding ceiling blocks a resume; a hand-paused task is unaffected."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Hand {uuid4().hex[:6]}"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "x"})).json()
    await client.post(f"/runs/{run['id']}/controls", json={"kind": "pause"})
    response = await client.post(f"/runs/{run['id']}/resume")
    assert response.status_code == 202


async def test_limits_can_be_changed_while_a_task_is_still_working(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner should be able to give a long task room before it hits the wall."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Live {uuid4().hex[:6]}"})).json()
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": "Assess demand",
                "budget": {"max_tokens": 100000},
            },
        )
    ).json()
    response = await client.post(
        f"/runs/{run['id']}/budget", json={"max_tokens": 500000, "max_minutes": 45}
    )
    assert response.status_code == 202
    assert response.json()["resumed"] is False  # It never stopped.
    usage = (await client.get(f"/runs/{run['id']}/usage")).json()
    assert usage["budget"]["max_tokens"] == 500000
    assert usage["budget"]["max_minutes"] == 45
    assert (await client.get(f"/runs/{run['id']}")).json()["status"] == "PENDING"


async def test_limits_of_a_finished_task_cannot_be_changed(client: httpx.AsyncClient) -> None:
    run_id = await stopped_run(client, status="COMPLETED")
    response = await client.post(f"/runs/{run_id}/budget", json={"max_tokens": 500000})
    assert response.status_code == 409
    assert response.json()["detail"] == "run_finished"


async def test_an_artifact_reads_as_a_document_not_a_payload(client: httpx.AsyncClient) -> None:
    """The owner's report: a finished task offered a JSON dump to read."""
    artifact_id = await artifact_of(client, DOCUMENT_PAYLOAD)
    document = (await client.get(f"/artifacts/{artifact_id}/document")).json()
    assert document["title"] == "拟议项目：KYC 调研", "the task's own words title the document"
    assert "<h2>Key findings</h2>" in document["html"]
    assert "Three vendors cover NL" in document["html"]
    assert "AFM guidance — https://afm.nl/kyc" in document["html"]
    assert "research_question" not in document["html"], "no raw field names on screen"


@pytest.mark.parametrize(
    ("fmt", "starts"),
    [("md", b"# "), ("html", b"<!doctype html>"), ("docx", b"PK"), ("xlsx", b"PK")],
)
async def test_a_finished_task_downloads_as_a_real_file(
    client: httpx.AsyncClient, fmt: str, starts: bytes
) -> None:
    artifact_id = await artifact_of(client, DOCUMENT_PAYLOAD)
    response = await client.get(f"/artifacts/{artifact_id}/download?format={fmt}")
    assert response.status_code == 200
    assert response.content.startswith(starts), f"{fmt} is not a real {fmt} file"
    assert len(response.content) > 200


async def test_a_download_names_the_file_for_the_task_in_its_own_script(
    client: httpx.AsyncClient,
) -> None:
    """A header holds latin-1 only, so a Chinese name needs RFC 5987 beside it."""
    artifact_id = await artifact_of(client, DOCUMENT_PAYLOAD)
    response = await client.get(f"/artifacts/{artifact_id}/download?format=docx")
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition, disposition
    assert "%E6%8B%9F%E8%AE%AE" in disposition, "the task's own name is kept, percent-encoded"
    ascii_part = disposition.split(";")[1]
    assert ascii_part.strip().startswith('filename="'), "an ASCII fallback is offered too"
    assert all(character in disposition for character in ".docx")


async def test_an_unsupported_download_format_is_refused(client: httpx.AsyncClient) -> None:
    artifact_id = await artifact_of(client, DOCUMENT_PAYLOAD)
    response = await client.get(f"/artifacts/{artifact_id}/download?format=exe")
    assert response.status_code == 422
    assert response.json()["detail"] == "unsupported_format"


async def test_a_dry_run_shows_what_would_be_sent_and_stores_nothing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beacon's boundary: a preview must not call the evaluator or change state."""
    project_id, run_id = await memory_run(client, monkeypatch)
    preview = (await client.post(f"/runs/{run_id}/memory/evaluate?dry_run=true")).json()
    assert preview["dry_run"] is True and preview["stored"] == 0
    assert preview["calls"] == len(preview["would_send"]) > 0
    for item in preview["would_send"]:
        assert set(item) == {"kind", "lesson", "evidence"}
    assert (await client.get(f"/projects/{project_id}/memory")).json() == []


async def test_only_an_approved_lesson_reaches_a_later_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probability ranks; the owner decides what an agent is told."""
    from app.project_context import build_run_context

    project_id, run_id = await memory_run(client, monkeypatch)
    await client.post(f"/runs/{run_id}/memory/evaluate")
    waiting = (await client.get(f"/projects/{project_id}/memory")).json()
    blocked = next(row for row in waiting if row["kind"] == "source_blocked")

    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    from app.db.models import Project as ProjectModel

    with factory() as session:
        project = session.get(ProjectModel, UUID(project_id))
        before = build_run_context(session, project, settings)
    assert before["lessons"] == []

    decided = await client.post(f"/memory/{blocked['id']}/decision", json={"approved": True})
    assert decided.status_code == 200
    with factory() as session:
        project = session.get(ProjectModel, UUID(project_id))
        after = build_run_context(session, project, settings)
    assert any("dnb.nl" in lesson for lesson in after["lessons"])

    # A decision is made once.
    again = await client.post(f"/memory/{blocked['id']}/decision", json={"approved": False})
    assert again.status_code == 409


async def test_a_run_that_learned_nothing_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Empty {uuid4().hex[:6]}"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "x"})).json()
    response = await client.post(f"/runs/{run['id']}/memory/evaluate")
    assert response.status_code == 422
    assert response.json()["detail"] == "nothing_learned"


async def test_chat_from_the_browser_starts_no_work_and_approves_nothing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It answers. Approvals and new work stay where they were."""
    submitted: list[Any] = []
    monkeypatch.setattr(client.app_state.worker, "submit", submitted.append)  # type: ignore[attr-defined]

    async def converse(
        self: Any, history: list[dict[str, str]], text: str, agent: Any = None, **kept: Any
    ):
        return "I can tell you, but approving is yours."

    monkeypatch.setattr("app.channels.inbound_office.SlackOffice.converse", converse)
    before = (await client.get("/runs?limit=50")).json()
    said = await client.post("/chat", json={"text": "Approve the pending proposal and start it"})
    assert said.status_code == 200
    after = (await client.get("/runs?limit=50")).json()
    assert len(after) == len(before)
    assert submitted == []
    pending = (await client.get("/approvals/pending")).json()
    assert all(row["approval"]["status"] == "PENDING" for row in pending)


async def test_a_lesson_kept_for_a_venture_reaches_its_other_projects(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "DNB answers 403" is true for every piece of fintech work, once kept."""
    from app.db.models import Project as ProjectModel
    from app.project_context import build_run_context

    _, inside, sibling, outside = await venture_and_projects(client)
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    run = (
        await client.post("/runs", json={"project_id": inside, "objective": "KYC vendors"})
    ).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    dispatch = repository.describe([UUID(run["id"])])[0][0]
    repository.record_stage(
        dispatch,
        "research",
        {
            "research_question": "q",
            "key_findings": ["f"],
            "evidence": ["e"],
            "implications": ["i"],
            "confidence": "medium",
            "sources": [{"title": "A", "url": "https://a.example"}],
            "blocked_sources": [{"url": "https://www.dnb.nl/en/", "status": "http_403"}],
        },
    )
    await client.post(f"/runs/{run['id']}/memory/evaluate")
    waiting = (await client.get(f"/projects/{inside}/memory")).json()
    lesson = next(row for row in waiting if row["kind"] == "source_blocked")

    kept = await client.post(
        f"/memory/{lesson['id']}/decision", json={"approved": True, "scope": "venture"}
    )
    assert kept.status_code == 200

    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    with factory() as session:

        def lessons_of(project_id: str) -> list[str]:
            project = session.get(ProjectModel, UUID(project_id))
            return build_run_context(session, project, settings)["lessons"]

        assert any("dnb.nl" in text for text in lessons_of(sibling)), "the business line learns"
        other = lessons_of(outside)
        assert not any("dnb.nl" in text for text in other), "another business does not"


async def test_a_lesson_kept_for_a_project_stays_there(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import Project as ProjectModel
    from app.project_context import build_run_context

    _, inside, sibling, _ = await venture_and_projects(client)
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    run = (await client.post("/runs", json={"project_id": inside, "objective": "x"})).json()
    repository = client.app_state.repository  # type: ignore[attr-defined]
    dispatch = repository.describe([UUID(run["id"])])[0][0]
    repository.record_stage(
        dispatch,
        "research",
        {
            "research_question": "q",
            "key_findings": ["f"],
            "evidence": ["e"],
            "implications": ["i"],
            "confidence": "medium",
            "sources": [{"title": "A", "url": "https://a.example"}],
            "blocked_sources": [{"url": "https://only-here.example", "status": "http_403"}],
        },
    )
    await client.post(f"/runs/{run['id']}/memory/evaluate")
    waiting = (await client.get(f"/projects/{inside}/memory")).json()
    lesson = next(row for row in waiting if row["kind"] == "source_blocked")
    await client.post(f"/memory/{lesson['id']}/decision", json={"approved": True})

    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    with factory() as session:
        project = session.get(ProjectModel, UUID(sibling))
        assert not any(
            "only-here" in text for text in build_run_context(session, project, settings)["lessons"]
        )


async def test_a_new_owner_can_see_a_finished_task_before_connecting_a_model(
    client: httpx.AsyncClient,
) -> None:
    """Sample mode: offered in setup, installed on request, removable."""
    steps = {step["key"]: step for step in (await client.get("/setup")).json()["steps"]}
    assert "sample" in steps, "setup does not offer a sample office"
    assert steps["sample"]["done"] is False
    assert steps["sample"]["optional"] is True

    made = (await client.post("/sample-office")).json()
    assert made["created"] is True

    # It reads as a finished task, and says in the document that it is not real.
    detail = (await client.get(f"/runs/{made['run_id']}")).json()
    assert detail["status"] == "COMPLETED"
    artifacts = (await client.get(f"/runs/{made['run_id']}/artifacts")).json()
    assert artifacts, "the sample task produced nothing to look at"
    document = (await client.get(f"/artifacts/{artifacts[0]['id']}/document")).json()
    assert "sample work" in str(document).lower()

    # Setup now knows, and pressing it again makes no second sample.
    steps = {step["key"]: step for step in (await client.get("/setup")).json()["steps"]}
    assert steps["sample"]["done"] is True
    assert (await client.post("/sample-office")).json()["created"] is False

    assert (await client.request("DELETE", "/sample-office")).json()["removed"] is True
    steps = {step["key"]: step for step in (await client.get("/setup")).json()["steps"]}
    assert steps["sample"]["done"] is False


async def test_a_build_being_written_shows_its_plan_its_step_and_its_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LB1/LB2: the middle of the build, and the page as it is."""
    _, run_id, _ = await building_run(client, monkeypatch)
    live = (await client.get(f"/runs/{run_id}/build/live")).json()
    assert live["building"] is True and live["agent"] == "Codex"
    assert live["plan"] == [{"text": "Write the page", "done": True}]
    assert live["now"] == "Running node --check app.js"
    [edit] = [step for step in live["steps"] if step["kind"] == "file"]
    assert edit["diff"] == "+<h1>"
    assert all(step["kind"] not in ("workspace", "session", "now") for step in live["steps"])
    assert live["preview"].startswith(f"/previews/{run_id}/")
    assert live["preview"].endswith("/live/index.html") and live["changed"] > 0


async def test_the_live_page_is_sandboxed_and_lets_the_owner_point(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run_id, _ = await building_run(client, monkeypatch)
    live = (await client.get(f"/runs/{run_id}/build/live")).json()
    page = await client.get(live["preview"])
    assert page.status_code == 200
    assert page.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "<h1>Colouring</h1>" in page.text and "officePicked" in page.text
    base = live["preview"].rsplit("/", 1)[0]
    # Encoded, so the server sees the climb out of the clone and refuses it itself.
    climbed = await client.get(f"{base}/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
    assert climbed.status_code == 404 and "root:" not in climbed.text
    wrong = live["preview"].replace(f"/previews/{run_id}/", f"/previews/{run_id}/x")
    assert (await client.get(wrong)).status_code == 404


async def test_the_owner_comments_while_it_builds_and_it_waits_for_the_agent(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LB3: queued for the agent's next step, with what was pointed at."""
    state, run_id, _ = await building_run(client, monkeypatch)
    sent = await client.post(
        f"/runs/{run_id}/build/notes",
        json={
            "text": "Make this blue",
            "mode": "next",
            "pointed": {"tag": "h1", "text": "Colouring", "selector": "body > h1"},
        },
    )
    assert sent.status_code == 201, sent.text
    assert (
        await client.post(f"/runs/{run_id}/build/notes", json={"mode": "pause"})
    ).status_code == 201
    empty = await client.post(f"/runs/{run_id}/build/notes", json={"mode": "now", "text": " "})
    assert empty.status_code == 422
    [note, pause] = state.repository.pending_build_notes(run_id)
    assert note["kind"] == "build_note" and note["text"].startswith("Make this blue\n")
    assert "pointed at <h1> showing “Colouring” (at body > h1)" in note["text"]
    assert pause["kind"] == "build_pause"
    state.repository.take_build_notes(run_id, [note["id"]])
    assert [n["kind"] for n in state.repository.pending_build_notes(run_id)] == ["build_pause"]
    live = (await client.get(f"/runs/{run_id}/build/live")).json()
    assert [(n["kind"], n["status"]) for n in live["notes"]] == [
        ("build_note", "APPLIED"),
        ("build_pause", "QUEUED"),
    ]


async def test_a_finished_task_takes_no_build_comments(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run_id = await finished_run(client, monkeypatch)
    refused = await client.post(f"/runs/{run_id}/build/notes", json={"text": "Blue"})
    assert (refused.status_code, refused.json()["detail"]) == (409, "not_building")


async def test_an_answer_is_a_page_at_a_private_link_with_its_charts(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SL5: read-only, by a link that names one answer, charts drawn."""
    _, run_id = await finished_run(client, monkeypatch, DASHBOARD)
    [artifact] = (await client.get(f"/runs/{run_id}/artifacts")).json()
    path = (await client.get(f"/artifacts/{artifact['id']}/page")).json()["path"]
    page = await client.get(path)
    assert page.status_code == 200
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert "Sign-ups tripled this year." in page.text and '<svg class="chart"' in page.text
    assert "<script" not in page.text
    other = path.rsplit("/", 1)[0] + "/" + "0" * 32
    assert (await client.get(other)).status_code == 404
    assert (await client.get(f"/pages/{uuid4()}/{path.rsplit('/', 1)[1]}")).status_code == 404


async def test_performance_shows_steps_reading_and_failures(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP1: from the office's own records, nothing sent anywhere."""
    from app.db.models import RunEvent

    _, run_id = await finished_run(client, monkeypatch)
    state = client.app_state  # type: ignore[attr-defined]
    run = UUID(run_id)
    for kind, node in (("step_started", "research"), ("step_completed", "research")):
        state.repository.add_event(run, kind, {"node": node})
    for read in (
        {"via": "direct", "ok": True, "ms": 800, "chars": 3000, "code": None},
        {"via": "browser", "ok": True, "ms": 4000, "chars": 2500, "code": None},
        {"via": "failed", "ok": False, "ms": 300, "chars": 0, "code": "fetch_rejected_403"},
    ):
        state.repository.add_event(run, "page_read", read)
    with state.session_factory() as session, session.begin():
        started, finished = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run, RunEvent.kind.in_(("step_started", "step_completed")))
            .order_by(RunEvent.id)
        ).all()
        finished.created_at = started.created_at + timedelta(seconds=95)
    state.repository.add_event(run, "lead_step", {"kind": "tool_call", "brief": 1})
    state.repository.add_event(run, "lead_step", {"kind": "tool_call", "brief": 1})
    state.repository.add_event(run, "lead_step", {"kind": "tool_call"})
    state.repository.add_event(
        run, "step_result", {"status": "done", "described": True, "unreturned": ["https://x.nl"]}
    )
    state.repository.add_event(run, "step_result", {"status": "limit", "unreturned": []})
    work = (await client.get("/performance/work?days=7")).json()
    assert work["lead"] == {
        "tasks_delegating": 1,
        "briefs": 2,
        "typical_briefs": 2,
        "typical_calls": 2,
        "ended": {"done": 1, "limit": 1},
        "fully_described": 1,
        "addresses_taken_out": 1,
    }
    [research] = [step for step in work["steps"] if step["step"] == "research"]
    assert (research["label"], research["typical_seconds"]) == ("Research", 95)
    assert work["pages"]["count"] >= 3 and work["pages"]["read"] >= 2
    assert {"via": "browser"}.items() <= next(
        way for way in work["pages"]["ways"] if way["via"] == "browser"
    ).items()
    assert any(f["code"] == "fetch_rejected_403" for f in work["pages"]["why_failed"])
    assert len(work["benchmark"]["questions"]) == 8


async def test_the_benchmark_starts_only_when_the_owner_says_it_may_cost(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    """WP1: paid work, so the press must carry its confirmation."""
    refused = await client.post("/performance/benchmark", json={})
    assert refused.status_code == 422 and no_dispatch == []
    started = await client.post("/performance/benchmark", json={"confirm": True})
    assert started.status_code == 202, started.text
    assert len(started.json()["started"]) == 8 and len(no_dispatch) == 8
    runs = (await client.get("/performance/work")).json()["benchmark"]["runs"]
    assert len(runs) == 8 and all(r["status"] == "PENDING" for r in runs)


async def test_a_follow_up_carries_what_the_earlier_task_found(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's report: a finished task was a dead end."""
    project_id, run_id = await finished_run(client, monkeypatch)
    jobs: list[Any] = []
    monkeypatch.setattr(client.app_state.worker, "submit", jobs.append)  # type: ignore[attr-defined]

    started = await client.post(
        f"/runs/{run_id}/follow-up",
        json={"objective": "Then what would the governance gap cost us to close?"},
    )
    assert started.status_code == 202
    child = started.json()
    assert child["continues_run_id"] == run_id
    assert child["project_id"] == project_id, "a follow-up stays in its project"

    told = "\n".join(jobs[0].context["continues"])
    assert "Apply this quarter" in told, "the follow-up was not told what was concluded"
    assert "No provider publishes its onboarding time" in told
    assert "EMI licence this quarter" in told, "it was not told what was asked before"
    assert "contradicts it" in told, "findings must be carried as found, not as settled"


async def test_a_follow_up_of_a_build_aims_at_its_next_version_not_a_report(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's report: asking a website for a change offered report shapes."""
    _, run_id = await finished_run(client, monkeypatch)
    client.app_state.repository.merge_state(  # type: ignore[attr-defined]
        UUID(run_id), {"build_folder": "site-1"}
    )
    jobs: list[Any] = []
    monkeypatch.setattr(client.app_state.worker, "submit", jobs.append)  # type: ignore[attr-defined]

    started = await client.post(
        f"/runs/{run_id}/follow-up", json={"objective": "Add a dinosaur theme"}
    )
    assert started.status_code == 202
    assert "working software" in jobs[0].context["deliverable"]
    assert "written report" not in jobs[0].context["deliverable"]


async def test_parent_and_follow_up_each_name_the_other(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Either direction alone leaves a reader stranded."""
    _, run_id = await finished_run(client, monkeypatch)
    child = (
        await client.post(f"/runs/{run_id}/follow-up", json={"objective": "And the cost?"})
    ).json()

    parent_thread = (await client.get(f"/runs/{run_id}/thread")).json()
    assert [item["run_id"] for item in parent_thread["followed_by"]] == [child["id"]]
    assert parent_thread["continues"] is None

    child_thread = (await client.get(f"/runs/{child['id']}/thread")).json()
    assert child_thread["continues"]["run_id"] == run_id
    # And it is named by a short title, not by its whole objective.
    assert len(child_thread["continues"]["title"]) <= 73
    assert "so that" not in child_thread["continues"]["title"]


async def test_a_task_still_working_is_steered_not_continued(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follow-up would duplicate work already in flight."""
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Live {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Still going"})
    ).json()
    refused = await client.post(f"/runs/{run['id']}/follow-up", json={"objective": "More"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "still_working"


async def test_a_task_is_named_by_a_short_title(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's report: the whole objective was set in bold as the title."""
    _, run_id = await finished_run(client, monkeypatch)
    run = (await client.get(f"/runs/{run_id}")).json()
    assert run["title"] == "Assess whether we can apply for the EMI licence this quarter"
    # And nothing is lost: the ask is still there in full.
    assert "so that the board can decide" in run["objective"]


async def test_a_task_is_named_the_moment_it_is_created(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run handed back nameless shows as a blank line until it is refetched."""
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Named {uuid4().hex[:6]}"})).json()
    created = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": "Draft the board update, so that October can be decided",
            },
        )
    ).json()
    assert created["title"] == "Draft the board update"

    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        row = session.get(RunRow, UUID(created["id"]))
        assert row is not None
        row.status = "COMPLETED"
        session.commit()

    follow = (
        await client.post(
            f"/runs/{created['id']}/follow-up", json={"objective": "And what would it cost?"}
        )
    ).json()
    assert follow["title"] == "And what would it cost?"

    # And the list names them too, without a query per run.
    listed = (await client.get(f"/runs?project_id={project['id']}")).json()
    assert all(row["title"] for row in listed), listed


async def test_a_server_whose_credential_is_gone_is_left_out_and_said_so(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import McpServer
    from app.policies.store import PolicyGate

    state = client.app_state  # type: ignore[attr-defined]
    server, project = await _granted_filings(client, monkeypatch)
    with state.session_factory() as session, session.begin():
        row = session.get(McpServer, UUID(server["id"]))
        row.credential_ref = "env:QUARTERDECK_TEST_MISSING_MCP_KEY"
    dispatch = state.repository.create(UUID(project["id"]), "look it up")
    assert await state.worker._mcp_tools(dispatch, PolicyGate(state.session_factory)) is None
    events = (await client.get(f"/runs/{dispatch.run_id}/events")).json()
    assert any(event.get("type", event.get("kind")) == "mcp_unavailable" for event in events), (
        events
    )


async def test_a_runs_critic_checks_with_the_same_granted_tools(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6c: the critic is handed the project's tools, and its calls are recorded."""
    from app.mcp.client import ToolResult
    from app.policies.store import PolicyGate
    from app.runtime.worker import build_agents

    state = client.app_state  # type: ignore[attr-defined]
    _, project = await _granted_filings(client, monkeypatch)
    dispatch = state.repository.create(UUID(project["id"]), "check it")
    from app.office_models import office_models

    settings = state.worker.settings
    agents = build_agents(office_models(settings, state.session_factory), settings)
    await state.worker._hand_tools(agents, dispatch, PolicyGate(state.session_factory))
    assert agents.critic.mcp is not None
    assert agents.critic.mcp is agents.researcher.mcp

    async def call(
        self: Any, url: str, token: Any, allow: Any, name: str, arguments: dict
    ) -> ToolResult:
        return ToolResult(content="confirmed", failed=False)

    monkeypatch.setattr("app.mcp.client.McpClient.call", call)
    server_name = agents.critic.mcp.servers[0].name
    assert await agents.critic.mcp.call(server_name, "search", {"q": "x"}) == "confirmed"
    events = (await client.get(f"/runs/{dispatch.run_id}/events")).json()
    assert any(event.get("kind", event.get("type")) == "mcp_call" for event in events), events


async def test_start_as_a_task_starts_it_once(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, reply, submitted = await _proposed(client, monkeypatch)
    path = _action_path(project_id, reply)
    started = await client.post(path, json={})
    assert started.status_code == 201, started.text
    run_id = started.json()["action"]["run_id"]
    run = (await client.get(f"/runs/{run_id}")).json()
    assert run["objective"] == "Read DNB's Q&A on simplified CDD."
    assert run["project_id"] == project_id
    assert len(submitted) == 1
    again = await client.post(path, json={})
    assert again.status_code == 409 and again.json()["detail"] == "already_done"
    assert len(submitted) == 1
    # The room shows the button as done, with the task it started.
    shown = (await _said(client, project_id))[-1]["actions"][0]
    assert shown["run_id"] == run_id


async def test_the_owner_may_reword_a_proposal_before_starting_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, reply, _ = await _proposed(client, monkeypatch)
    started = await client.post(
        _action_path(project_id, reply), json={"objective": "Read DNB's CDD guidance for EMIs."}
    )
    run = (await client.get(f"/runs/{started.json()['action']['run_id']}")).json()
    assert run["objective"] == "Read DNB's CDD guidance for EMIs."


# A task can be given its own title; without one, a short one is derived.


async def test_a_task_keeps_the_title_it_was_given(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Titled {uuid4().hex[:6]}"})).json()
    ask = "Please research what the Wwft requires of an EMI for customer due diligence."
    run = (
        await client.post(
            "/runs",
            json={"project_id": project["id"], "objective": ask, "title": " EMI CDD check "},
        )
    ).json()
    assert run["title"] == "EMI CDD check"
    assert (await client.get(f"/runs/{run['id']}")).json()["title"] == "EMI CDD check"
    listed = (await client.get("/runs?limit=50")).json()
    assert next(r for r in listed if r["id"] == run["id"])["title"] == "EMI CDD check"
    board = (await client.get("/office/board")).json()
    cards = [card for column in board["columns"] for card in column.get("cards", [])]
    assert any(card.get("title") == "EMI CDD check" for card in cards)


# The office shares what it knows.


async def test_a_task_shows_what_it_was_told_not_what_its_project_holds_now(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    project_id, first = await project_with_work(client, f"Told {uuid4().hex[:6]}")
    follow = await client.post(
        f"/runs/{first.run_id}/follow-up", json={"objective": "Rank the six industries above"}
    )
    assert follow.status_code == 202, follow.text
    follow_id = follow.json()["id"]
    # Finished after the follow-up started: the follow-up was never told it.
    later_project, _ = await project_with_work(client, f"Later {uuid4().hex[:6]}")
    del later_project

    told = (await client.get(f"/runs/{follow_id}/context")).json()
    names = [part["name"] for part in told["parts"]]
    assert "The task it continues" in names, names
    continued = next(p for p in told["parts"] if p["name"] == "The task it continues")
    assert any("rec" in item for item in continued["items"])
    assert told["run_id"] == follow_id and told["project_id"] == project_id
    assert (await client.get(f"/runs/{uuid4()}/context")).status_code == 404


async def test_related_work_from_another_project_reaches_a_new_task(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    tag = uuid4().hex[:6]
    kyc = await _finished(
        client, f"KYC {tag}", "Which KYC vendors suit a Dutch EMI?", f"Use eIDAS vendors {tag}"
    )
    await _finished(client, f"Dogs {tag}", "Market dog food", f"Sell kibble online {tag}")
    project = (await client.post("/projects", json={"name": f"New {tag}"})).json()
    run = (
        await client.post(
            "/runs", json={"project_id": project["id"], "objective": "Plan EMI onboarding"}
        )
    ).json()
    dispatch, objective, context = state.repository.describe([UUID(run["id"])])[0]

    judge = _RelevanceJudge(f"eIDAS vendors {tag}")
    shared = await state.worker._share_related(dispatch, objective, context, judge)
    assert len(shared) == 1 and f"Use eIDAS vendors {tag}" in shared[0]
    assert "Which KYC vendors suit a Dutch EMI?" in shared[0] and f"KYC {tag}" in shared[0]
    assert not any(str(run["id"]) in passage for passage in judge.asked)

    told = (await client.get(f"/runs/{run['id']}/context")).json()
    related = next(p for p in told["parts"] if p["name"] == "Related work in the office")
    assert related["items"] == shared
    events = (await client.get(f"/runs/{run['id']}/events")).json()
    recorded = [e for e in events if e.get("kind") == "related_work"]
    assert recorded and recorded[0]["payload"]["runs"] == [str(kyc.run_id)]


async def test_the_task_it_continues_is_not_told_twice(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    tag = uuid4().hex[:6]
    parent = await _finished(client, f"KYC {tag}", "KYC vendors", f"Use eIDAS vendors {tag}")
    follow = (
        await client.post(f"/runs/{parent.run_id}/follow-up", json={"objective": "Now rank them"})
    ).json()
    dispatch, objective, context = state.repository.describe([UUID(follow["id"])])[0]
    judge = _RelevanceJudge(f"eIDAS vendors {tag}")
    assert await state.worker._share_related(dispatch, objective, context, judge) == []


async def test_a_new_task_can_build_on_a_task_in_another_project(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X1 stage 3: the owner chose to build on it; the project line is theirs to cross."""
    from app.db.models import Run

    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    tag = uuid4().hex[:6]
    earlier = await _finished(client, f"Slack Q {tag}", "Six industries?", f"Retail first {tag}")
    home = (await client.post("/projects", json={"name": f"Home {tag}"})).json()
    run = await client.post(
        "/runs",
        json={
            "project_id": home["id"],
            "objective": "Rank the six industries above",
            "continues_run_id": str(earlier.run_id),
        },
    )
    assert run.status_code in (200, 201, 202), run.text
    with state.session_factory() as session:
        row = session.get(Run, UUID(run.json()["id"]))
        assert row.continues_run_id == earlier.run_id
        assert any(f"Retail first {tag}" in line for line in row.state_json["context"]["continues"])
    missing = await client.post(
        "/runs",
        json={"project_id": home["id"], "objective": "x", "continues_run_id": str(uuid4())},
    )
    assert missing.status_code == 404


# Rework a finished task.


async def test_a_finished_task_can_be_reworked_in_place(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import Artifact

    state = client.app_state  # type: ignore[attr-defined]
    resumed: list[tuple[UUID, dict]] = []
    monkeypatch.setattr(
        state.worker,
        "resume_for_review",
        lambda run_id, override: resumed.append((run_id, override)),
    )
    _, done = await project_with_work(client, f"Rework {uuid4().hex[:6]}")
    reworked = await client.post(
        f"/runs/{done.run_id}/rework",
        json={"comment": "Add the EU vendors", "revision_target": "research"},
    )
    assert reworked.status_code == 202, reworked.text
    assert reworked.json()["status"] == "PENDING"
    [(run_id, override)] = resumed
    assert run_id == done.run_id
    assert override["critique"]["revision_target"] == "research"
    assert override["critique"]["required_corrections"] == ["Add the EU vendors"]
    with state.session_factory() as session:
        kinds = {row.type for row in session.query(Artifact).filter(Artifact.run_id == done.run_id)}
    assert "human_review" in kinds
    events = (await client.get(f"/runs/{done.run_id}/events")).json()
    assert any(e.get("kind") == "rework_requested" for e in events)


async def test_rework_is_only_for_finished_tasks(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "resume_for_review", lambda *a: None)
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Busy {uuid4().hex[:6]}"})).json()
    running = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Still going"})
    ).json()
    busy = await client.post(f"/runs/{running['id']}/rework", json={"comment": "Redo it"})
    assert busy.status_code == 409 and busy.json()["detail"] == "not_finished"
    assert (await client.post(f"/runs/{uuid4()}/rework", json={"comment": "x"})).status_code == 404


async def test_a_rework_without_a_note_improves_what_the_review_raised(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RW1: one press; the office writes the note from the task's own record."""
    state = client.app_state  # type: ignore[attr-defined]
    resumed: list[dict] = []
    monkeypatch.setattr(
        state.worker, "resume_for_review", lambda run_id, override: resumed.append(override)
    )
    _, done = await project_with_work(client, f"Again {uuid4().hex[:6]}")
    state.repository.merge_state(
        done.run_id,
        {
            "last_critique": {
                "verdict": "PASS",
                "issues": [
                    {
                        "issue": "Only one class found",
                        "why_it_matters": "x",
                        "fix": "search in Dutch",
                    }
                ],
                "writing_fixes": ["Lead with the classes"],
            },
            "synthesis": {"not_checked": "Prices for B are not published."},
        },
    )
    reworked = await client.post(f"/runs/{done.run_id}/rework", json={})
    assert reworked.status_code == 202, reworked.text
    note = resumed[0]["critique"]["required_corrections"][0]
    assert "Only one class found (fix: search in Dutch)" in note
    assert "Prices for B are not published." in note and "Lead with the classes" in note


async def test_rework_respects_a_paused_office(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "resume_for_review", lambda *a: None)
    _, done = await project_with_work(client, f"Paused {uuid4().hex[:6]}")
    await client.put("/settings", json={"pause_new_tasks": True})
    try:
        refused = await client.post(
            f"/runs/{done.run_id}/rework",
            json={"comment": "Redo the research", "revision_target": "research"},
        )
        assert refused.status_code == 409 and refused.json()["detail"] == "office_paused"
    finally:
        await client.put("/settings", json={"pause_new_tasks": False})


async def test_reworking_a_task_that_never_planned_starts_it_again(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update as sql_update

    state = client.app_state  # type: ignore[attr-defined]
    jobs: list[Any] = []
    monkeypatch.setattr(state.worker, "submit", jobs.append)
    resumed: list[Any] = []
    monkeypatch.setattr(state.worker, "resume_for_review", lambda *a: resumed.append(a))
    project = (await client.post("/projects", json={"name": f"Unplanned {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Plan it"})
    ).json()
    jobs.clear()
    with state.session_factory() as session, session.begin():
        session.execute(
            sql_update(RunRow)
            .where(RunRow.id == UUID(run["id"]))
            .values(status="FAILED", error="planner:provider_rejected_request")
        )
    reworked = await client.post(
        f"/runs/{run['id']}/rework",
        json={"comment": "Focus on the EU", "revision_target": "research"},
    )
    assert reworked.status_code == 202, reworked.text
    assert resumed == [], "there is no plan to revise against"
    [job] = jobs
    assert job.resume is False and job.dispatch.run_id == UUID(run["id"])
    assert job.context.get("project_id") == project["id"], "it keeps what it was told"
    controls = state.repository.controls(UUID(run["id"]))
    assert [(c["kind"], c["text"]) for c in controls] == [("instruction", "Focus on the EU")]


async def test_two_projects_with_one_name_are_told_apart(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.project_routing import catalog

    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    name = f"Twin {uuid4().hex[:6]}"
    older = (await client.post("/projects", json={"name": name})).json()
    from app.db.models import Project

    with state.session_factory() as session, session.begin():
        session.add(Project(name=name, description="the second one"))
    newer_id = None
    with state.session_factory() as session:
        newer_id = next(
            row.id
            for row in session.query(Project).filter(Project.name == name)
            if str(row.id) != older["id"]
        )
    await client.post("/runs", json={"project_id": str(newer_id), "objective": "Recent work"})
    with state.session_factory() as session:
        found = catalog(session)
    twins = [label for label in found.ids if label.startswith(name)]
    assert len(twins) == 2 and len(set(twins)) == 2
    assert found.ids[name] == newer_id, "the plain name means the most recently active"


async def test_a_tasks_workflow_is_drawn_from_what_it_recorded(
    client: httpx.AsyncClient,
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Flow {uuid4().hex[:6]}"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Compare KYC vendors")
    plan = {
        "steps": [
            {"id": "rules", "kind": "research", "brief": "Rules", "depends_on": []},
            {"id": "report", "kind": "write", "brief": "Write", "depends_on": ["rules"]},
        ]
    }
    _set_run(state, dispatch.run_id, status="RUNNING", state_json={"plan": plan, "step_status": {}})
    state.repository.add_event(dispatch.run_id, "step_started", {"node": "plan"})
    state.repository.add_event(dispatch.run_id, "step_completed", {"node": "plan"})
    state.repository.add_event(dispatch.run_id, "step_started", {"node": "research"})

    drawn = await client.get(f"/runs/{dispatch.run_id}/flow")
    assert drawn.status_code == 200, drawn.text
    body = drawn.json()
    assert body["phase"] == "workers"
    assert [(s["id"], s["status"]) for s in body["steps"]] == [
        ("rules", "running"),
        ("report", "pending"),
    ]
    assert [row["text"] for row in body["log"]][-1] == "step started"
    assert (await client.get(f"/runs/{uuid4()}/flow")).status_code == 404


async def test_why_an_output_was_rejected_reaches_the_record_and_the_log(
    client: httpx.AsyncClient,
) -> None:
    from app.events import InvocationEvent

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Why {uuid4().hex[:6]}"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Compare trends")
    state.repository.record_usage(
        InvocationEvent(
            run_id=dispatch.run_id,
            project_id=UUID(project["id"]),
            role="researcher",
            model_alias="0002",
            status="rejected",
            attempts=3,
            error_code="malformed_structured_output",
            error_detail="attempt 1: not valid JSON: it ends before the object is complete",
        )
    )
    log = (await client.get(f"/runs/{dispatch.run_id}/flow")).json()["log"]
    assert (
        "researcher",
        "output rejected after 3 tries · it ends before the object is complete",
    ) in [(row["actor"], row["text"]) for row in log]


async def test_the_room_is_kept_on_the_tasks_record_and_shown_with_its_flow(
    client: httpx.AsyncClient,
) -> None:
    from app.orchestrator.dispatcher import RecordedBoard

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Room {uuid4().hex[:6]}"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Compare vendors")
    board = RecordedBoard(state.repository, dispatch.run_id)
    claim = {
        "key": "k1",
        "kind": "claim",
        "author_role": "researcher",
        "step_id": "a",
        "text": "Taking: rules",
    }
    board.post(claim)
    board.post(claim)  # said again after a pause
    board.post(
        {
            "key": "k2",
            "kind": "challenge",
            "author_role": "critic",
            "to_step": "a",
            "text": "Primary?",
        }
    )
    posts = board.read()
    assert [p["kind"] for p in posts] == ["claim", "challenge"]
    room = (await client.get(f"/runs/{dispatch.run_id}/flow")).json()["room"]
    assert room["count"] == 2 and room["posts"][1]["to_step"] == "a"


async def test_the_owner_posts_in_the_tasks_room(client: httpx.AsyncClient) -> None:
    """T4: the owner speaks in the room; a post to a step names a step the plan has."""
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Say {uuid4().hex[:6]}"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Compare vendors")
    plan = {"steps": [{"id": "rules", "kind": "research", "brief": "Read the rules"}]}
    _set_run(state, dispatch.run_id, status="RUNNING", state_json={"plan": plan})
    said = await client.post(
        f"/runs/{dispatch.run_id}/room",
        json={"text": "  Use the 2026 rules.  ", "to_step": "rules"},
    )
    assert said.status_code == 201, said.text
    assert said.json()["kind"] == "owner" and said.json()["to_step"] == "rules"
    assert said.json()["text"] == "Use the 2026 rules."
    everyone = await client.post(f"/runs/{dispatch.run_id}/room", json={"text": "Keep it short."})
    assert everyone.status_code == 201 and everyone.json()["to_step"] is None
    room = (await client.get(f"/runs/{dispatch.run_id}/flow")).json()["room"]
    assert [p["text"] for p in room["posts"]] == ["Use the 2026 rules.", "Keep it short."]
    unknown = await client.post(
        f"/runs/{dispatch.run_id}/room", json={"text": "x", "to_step": "nope"}
    )
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown_step"
    blank = await client.post(f"/runs/{dispatch.run_id}/room", json={"text": "   "})
    assert blank.status_code == 422


async def test_a_question_about_a_task_is_answered_with_its_room_in_hand(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project_id, fake = await _room_with_team(client, monkeypatch)
    dispatch = state.repository.create(UUID(project_id), "Compare agent frameworks")
    _team_post(state, dispatch.run_id, "c", "challenge", "critic", "No AutoGen source at all.")
    sent = await client.post(
        f"/projects/{project_id}/room",
        json={"text": "@Iris why was AutoGen left out?", "run_id": str(dispatch.run_id)},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["run_id"] == str(dispatch.run_id)
    said = await _said(client, project_id)
    assert [m["run_id"] for m in said] == [str(dispatch.run_id)] * 2, "the reply is the task's too"
    prompt = fake.asked[0][1]
    assert "No AutoGen source at all." in prompt and "Compare agent frameworks" in prompt
    talk = (await client.get(f"/runs/{dispatch.run_id}/flow")).json()["room"]["talk"]
    assert [m["author"]["kind"] for m in talk] == ["owner", "agent"]


# A task's lineage, read from what the office already records.


async def test_a_tasks_lineage_reads_both_ways(client: httpx.AsyncClient) -> None:
    from app.db.models import Entity, EntityFact, Objective, ProjectMemory, Venture

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Line {uuid4().hex[:6]}"})).json()
    other = (await client.post("/projects", json={"name": f"Elsewhere {uuid4().hex[:6]}"})).json()
    first = state.repository.create(UUID(project["id"]), "Which EMI licence fits?")
    middle = state.repository.create(UUID(project["id"]), "And what does DNB ask for it?")
    last = state.repository.create(UUID(other["id"]), "Draft the DNB application outline")
    side = state.repository.create(UUID(other["id"]), "PSD2 safeguarding rules")
    later = state.repository.create(UUID(other["id"]), "Safeguarding account options")
    with state.session_factory() as session, session.begin():
        line = Venture(name=f"Payments {uuid4().hex[:6]}")
        session.add(line)
        session.flush()
        goal = Objective(venture_id=line.id, title="Licence by Q1")
        subject = Entity(venture_id=line.id, name="DNB")
        session.add_all([goal, subject])
        session.flush()
        session.execute(
            update(RunRow)
            .where(RunRow.id == middle.run_id)
            .values(continues_run_id=first.run_id, objective_id=goal.id)
        )
        session.execute(
            update(RunRow).where(RunRow.id == last.run_id).values(continues_run_id=middle.run_id)
        )
        session.add(
            ProjectMemory(
                project_id=UUID(project["id"]),
                run_id=middle.run_id,
                kind="source",
                lesson="DNB's own pages beat summaries.",
                lesson_digest=uuid4().hex,
            )
        )
        session.add(
            EntityFact(
                entity_id=subject.id,
                run_id=middle.run_id,
                fact="DNB asks for a business plan.",
                fact_digest=uuid4().hex,
            )
        )
    state.repository.add_event(middle.run_id, "related_work", {"runs": [str(side.run_id)]})
    state.repository.add_event(later.run_id, "related_work", {"runs": [str(middle.run_id)]})

    seen = (await client.get(f"/runs/{middle.run_id}/lineage")).json()
    assert [r["run_id"] for r in seen["built_on"]] == [str(first.run_id)]
    assert [r["run_id"] for r in seen["built_upon"]] == [str(last.run_id)]
    assert seen["built_upon"][0]["project"] == other["name"], "across projects"
    assert [r["run_id"] for r in seen["told_about"]] == [str(side.run_id)]
    assert [r["run_id"] for r in seen["told_to"]] == [str(later.run_id)]
    assert [x["text"] for x in seen["taught"]] == ["DNB's own pages beat summaries."]
    assert [(x["subject"], x["text"]) for x in seen["noted"]] == [
        ("DNB", "DNB asks for a business plan.")
    ]
    assert seen["serves"] == "Licence by Q1"
    # The whole chain, from either end.
    top = (await client.get(f"/runs/{last.run_id}/lineage")).json()
    assert [r["run_id"] for r in top["built_on"]] == [str(middle.run_id), str(first.run_id)]
    assert [r["hops"] for r in top["built_on"]] == [1, 2]


async def test_a_task_with_no_links_has_an_empty_lineage(client: httpx.AsyncClient) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Alone {uuid4().hex[:6]}"})).json()
    alone = state.repository.create(UUID(project["id"]), "A question on its own")
    seen = (await client.get(f"/runs/{alone.run_id}/lineage")).json()
    assert seen["built_on"] == [] and seen["built_upon"] == [] and seen["taught"] == []
    assert seen["serves"] is None
    assert (await client.get(f"/runs/{uuid4()}/lineage")).status_code == 404


# A follow-up is told the earlier links of its line of work.


async def test_a_follow_up_is_told_the_earlier_links_that_bear_on_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    tag = uuid4().hex[:6]
    far = await _finished(client, f"Far {tag}", "Dog food pricing", f"Kibble wins {tag}")
    grand = await _finished(
        client, f"Grand {tag}", "Which EMI licence fits?", f"Grandparent says EMI {tag}"
    )
    parent = await _finished(client, f"Parent {tag}", "What does DNB ask?", f"Parent says {tag}")
    with state.session_factory() as session, session.begin():
        session.execute(
            update(RunRow).where(RunRow.id == grand.run_id).values(continues_run_id=far.run_id)
        )
        session.execute(
            update(RunRow).where(RunRow.id == parent.run_id).values(continues_run_id=grand.run_id)
        )
        # Waiting on the owner's review: never related work, still in the line.
        session.execute(
            update(RunRow).where(RunRow.id == grand.run_id).values(status="NEEDS_HUMAN_REVIEW")
        )
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": str(parent.project_id),
                "objective": "Draft the EMI application",
                "continues_run_id": str(parent.run_id),
            },
        )
    ).json()
    dispatch, objective, context = state.repository.describe([UUID(run["id"])])[0]
    # Relevant: the grandparent; not the far dog-food task.
    judge = _RelevanceJudge(f"Grandparent says EMI {tag}")
    await state.worker._share_related(dispatch, objective, context, judge)
    told = (await client.get(f"/runs/{run['id']}/context")).json()
    parts = {p["name"]: p["items"] for p in told["parts"]}
    related = parts.get("Related work in the office", [])
    line = parts.get("Earlier in this line of work", [])
    # Told once: as related work, or as an earlier link, never both.
    grand_told = [t for t in related + line if f"Grandparent says EMI {tag}" in t]
    assert len(grand_told) == 1
    assert not any(f"Kibble wins {tag}" in t for t in line), "an irrelevant link is left out"
    assert not any(f"Parent says {tag}" in t for t in line), "the parent is told as today"
    assert [t for t in line if f"Grandparent says EMI {tag}" in t], line
    assert "1 task before the one it continues" in line[0]
    events = (await client.get(f"/runs/{run['id']}/events")).json()
    told_event = [e for e in events if e.get("kind") == "lineage_told"]
    assert told_event and told_event[0]["payload"]["runs"] == [str(grand.run_id)]


async def test_a_task_with_no_line_of_work_is_told_what_it_was_before(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    monkeypatch.setattr(state.worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": f"Alone {uuid4().hex[:6]}"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Alone"})
    ).json()
    dispatch, objective, context = state.repository.describe([UUID(run["id"])])[0]
    await state.worker._share_related(dispatch, objective, context, _RelevanceJudge("Alone"))
    told = (await client.get(f"/runs/{run['id']}/context")).json()
    assert "Earlier in this line of work" not in {p["name"] for p in told["parts"]}


async def test_the_answer_beside_the_discussion_lists_its_versions(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    assert answer["type"] == "synthesis" and answer["current"]["version"] == 1
    assert [v["version"] for v in answer["versions"]] == [1]
    document = (await client.get(f"/artifacts/{answer['current']['artifact_id']}/document")).json()
    assert "EUR 5,000" in document["html"]


async def test_a_commented_passage_is_revised_into_a_new_version(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as json_module

    topic, fake, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    revised = {
        **ANSWER,
        "summary": "Published ranges run from EUR 3,500 to EUR 12,000 (three sources).",
    }
    fake.answers["Cato"] = json_module.dumps(
        {
            "revised": revised,
            "note": "Replaced the typical price with the published ranges.",
            "needs_research": False,
        }
    )
    sent = await client.post(
        f"/topics/{topic['id']}/revise",
        json={
            "run_id": str(run_id),
            "quote": "A typical engagement costs about EUR 5,000.",
            "comment": "The evidence here is thin.",
        },
    )
    assert sent.status_code == 202, sent.text
    asked = fake.asked[-1][1]
    assert "The evidence here is thin." in asked and "EUR 5,000" in asked
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    assert answer["current"]["version"] == 2
    assert answer["versions"][-1]["note"] == "Replaced the typical price with the published ranges."
    document = (await client.get(f"/artifacts/{answer['current']['artifact_id']}/document")).json()
    assert "EUR 3,500 to EUR 12,000" in document["html"]
    first = (await client.get(f"/artifacts/{answer['versions'][0]['artifact_id']}/document")).json()
    assert "EUR 5,000" in first["html"], "the earlier version is kept"
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Version 2")


# A task that crashes ends as failed, once, and an ended task is left alone.


async def test_a_crashed_task_is_failed_once_and_an_ended_one_is_left(
    client: httpx.AsyncClient,
) -> None:
    from app.runtime.worker import CRASHABLE

    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": f"Crash {uuid4().hex[:6]}"})).json()
    dispatch = state.repository.create(UUID(project["id"]), "Check the weather")
    code = "unexpected_error:ValidationError"
    assert state.repository.fail(dispatch.run_id, None, code, only_from=CRASHABLE) is True
    run = (await client.get(f"/runs/{dispatch.run_id}")).json()
    assert run["status"] == "FAILED" and run["error"] == code
    assert state.repository.fail(dispatch.run_id, None, code, only_from=CRASHABLE) is False


async def test_the_owners_edit_is_the_next_version_and_every_file_carries_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    shown = (await client.get(f"/artifacts/{answer['current']['artifact_id']}/document")).json()
    edited = _edited(shown["document"], "Start with a **paid** diagnosis.")
    saved = await client.put(f"/runs/{run_id}/answer", json={"base_version": 1, "document": edited})
    assert saved.status_code == 200, saved.text
    current = saved.json()["current"]
    assert current["version"] == 2 and current["by"] == "owner"
    page = (await client.get(f"/artifacts/{current['artifact_id']}/document")).json()
    assert "Start with a <strong>paid</strong> diagnosis." in page["html"]
    assert "How it got there" in page["html"], "the working is kept beneath the edit"
    markdown = await client.get(f"/artifacts/{current['artifact_id']}/download?format=md")
    assert "Start with a **paid** diagnosis." in markdown.text
    first = (await client.get(f"/artifacts/{answer['current']['artifact_id']}/document")).json()
    assert "human-delivered" in first["html"], "the agent's version is kept"


async def test_an_edit_made_on_an_older_version_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    shown = (await client.get(f"/artifacts/{answer['current']['artifact_id']}/document")).json()
    edited = _edited(shown["document"], "Mine.")
    first = await client.put(f"/runs/{run_id}/answer", json={"base_version": 1, "document": edited})
    assert first.status_code == 200
    stale = await client.put(f"/runs/{run_id}/answer", json={"base_version": 1, "document": edited})
    assert stale.status_code == 409 and stale.json()["detail"] == "newer_version:2"


async def test_a_task_says_what_it_was_asked_to_come_to(client: httpx.AsyncClient) -> None:
    """The editor opens a deck as slides; the run must say it is one."""
    project = (await client.post("/projects", json={"name": f"Deck {uuid4().hex[:6]}"})).json()
    run = (
        await client.post(
            "/runs",
            json={
                "project_id": project["id"],
                "objective": "Quarterly update",
                "deliverable": {"shape": "slides", "format": "pptx"},
            },
        )
    ).json()
    detail = (await client.get(f"/runs/{run['id']}")).json()
    assert detail["deliverable"] == {"shape": "slides", "format": "pptx"}


async def test_a_malformed_edit_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, _, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    refused = await client.put(
        f"/runs/{run_id}/answer",
        json={"base_version": 1, "document": {"title": "t", "sections": [{"blocks": "x"}]}},
    )
    assert refused.status_code == 422
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    assert answer["current"]["version"] == 1


async def test_a_passage_is_rewritten_as_a_suggestion_and_its_cost_recorded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as json_module

    topic, fake, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    fake.answers["Cato"] = json_module.dumps(
        {"text": "Begin with a diagnosis a person delivers.", "note": "Plainer words."}
    )
    sent = await client.post(
        f"/runs/{run_id}/answer/rewrite",
        json={
            "passage": "Start with a human-delivered diagnosis.",
            "instruction": "Plainer, please.",
        },
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["text"] == "Begin with a diagnosis a person delivers."
    asked = fake.asked[-1][1]
    assert "Plainer, please." in asked and "human-delivered" in asked
    answer = (await client.get(f"/runs/{run_id}/answer")).json()
    assert answer["current"]["version"] == 1, "a suggestion changes nothing by itself"
    said = await _said_in(client, topic["id"])
    assert said[-1]["text"].startswith("Suggested a rewrite")


async def test_no_rewrite_is_made_while_chat_is_switched_off(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic, fake, _ = await _topic_with(client, monkeypatch, "do")
    run_id = await _delivered_in(client, topic["id"], ANSWER)
    monkeypatch.setattr("app.revisions.paused", lambda session, settings, what: what == "chat")
    asked_before = len(fake.asked)
    refused = await client.post(
        f"/runs/{run_id}/answer/rewrite", json={"passage": "x", "instruction": "y"}
    )
    assert refused.status_code == 409 and refused.json()["detail"] == "chat_paused"
    assert len(fake.asked) == asked_before, "no model is called"


async def test_finishing_a_task_at_its_ceiling_resumes_it_to_write_the_answer(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6 Oct: the only way out of a ceiling was more money or nothing at all."""
    from tests.unit.test_graph import RESEARCH

    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    repository = client.app_state.repository  # type: ignore[attr-defined]
    dispatch = repository.describe([UUID(run_id)])[0][0]
    repository.record_stage(dispatch, "research", RESEARCH)
    response = await client.post(f"/runs/{run_id}/finish")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "PENDING"
    usage = (await client.get(f"/runs/{run_id}/usage")).json()
    assert usage["budget"]["max_tokens"] > 397284, "room for the last writing pass"
    assert (await client.get(f"/runs/{run_id}/question")).json() is None


async def test_a_task_with_nothing_done_cannot_be_finished(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await budget_paused_run(client, monkeypatch, {"max_tokens": 400000})
    response = await client.post(f"/runs/{run_id}/finish")
    assert response.status_code == 409
    assert response.json()["detail"] == "nothing_to_finish"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "PAUSED"


async def test_a_task_is_told_the_owners_day_as_far_as_they_allow(
    client: httpx.AsyncClient,
) -> None:
    """CAL3: titles and times by default; the event's notes never reach the task."""
    calendar_link_server(client, calendar_soon("Board review"))
    await client.post(
        "/calendars", json={"kind": "ics", "provider": "google", "url": "https://c.example/a.ics"}
    )
    project = (await client.post("/projects", json={"name": "Calendar work"})).json()
    run = (
        await client.post("/runs", json={"project_id": project["id"], "objective": "Plan the week"})
    ).json()
    told = (await client.get(f"/runs/{run['id']}/context")).json()
    [schedule] = [part for part in told["parts"] if part["name"] == "Your calendar"]
    assert "Board review" in schedule["items"][0]
    assert "4242" not in json.dumps(told)


async def test_a_prep_brief_is_offered_and_starts_only_on_the_owners_press(
    client: httpx.AsyncClient,
) -> None:
    calendar_link_server(client, calendar_soon("Board review"))
    await client.post(
        "/calendars", json={"kind": "ics", "provider": "google", "url": "https://c.example/a.ics"}
    )
    offer = (await client.get("/calendars/day")).json()["prep"]
    assert offer["title"] == "Board review" and offer["cost"] == 0.3
    runs_before = len((await client.get("/runs")).json())
    started = await client.post(f"/calendars/prep/{offer['id']}/start", json={"when": "16:00"})
    assert started.status_code == 202, started.text
    run = (await client.get(f"/runs/{started.json()['run_id']}")).json()
    assert "Board review" in run["objective"] and "16:00" in run["objective"]
    usage = (await client.get(f"/runs/{run['id']}/usage")).json()
    assert usage["budget"]["max_cost_usd"] == 0.3
    assert len((await client.get("/runs")).json()) == runs_before + 1
    assert (await client.get("/calendars/day")).json()["prep"] is None, "answered once"


async def test_the_office_browser_is_off_until_the_owner_turns_it_on(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """CU2, 8 Oct: tasks are told the browser is on only when it is, and runs."""
    import time as clock

    from app.browser.service import HEARTBEAT

    settings = client.settings  # type: ignore[attr-defined]
    exchange = tmp_path / "browser-exchange"
    exchange.mkdir()
    (exchange / HEARTBEAT).write_text(str(clock.time()))
    settings.browser_exchange = exchange
    view = (await client.get("/settings")).json()
    assert view["settings"]["browser_on"] is False and view["browser_running"] is True
    project = (await client.post("/projects", json={"name": "Browsing"})).json()
    off = (await client.post("/runs", json={"project_id": project["id"], "objective": "a"})).json()
    with client.app_state.session_factory() as session:  # type: ignore[attr-defined]
        told = session.get(RunRow, UUID(off["id"])).state_json["context"]
    assert told["browser"] is False
    assert (await client.put("/settings", json={"browser_on": True})).status_code == 200
    on = (await client.post("/runs", json={"project_id": project["id"], "objective": "b"})).json()
    with client.app_state.session_factory() as session:  # type: ignore[attr-defined]
        told = session.get(RunRow, UUID(on["id"])).state_json["context"]
    assert told["browser"] is True and told["browse_asked"] is False
    asked = (
        await client.post(
            "/runs", json={"project_id": project["id"], "objective": "c", "use_browser": True}
        )
    ).json()
    with client.app_state.session_factory() as session:  # type: ignore[attr-defined]
        told = session.get(RunRow, UUID(asked["id"])).state_json["context"]
    assert told["browse_asked"] is True, "CU5: the owner asked for the browser"
    await client.put("/settings", json={"browser_on": False})
    ignored = (
        await client.post(
            "/runs", json={"project_id": project["id"], "objective": "d", "use_browser": True}
        )
    ).json()
    with client.app_state.session_factory() as session:  # type: ignore[attr-defined]
        told = session.get(RunRow, UUID(ignored["id"])).state_json["context"]
    assert told["browse_asked"] is False, "asked for while off, the browser is not promised"


async def test_a_browser_screenshot_is_served_to_the_owner_only_by_its_number(
    client: httpx.AsyncClient,
) -> None:
    settings = client.settings  # type: ignore[attr-defined]
    run_id = uuid4()
    folder = settings.workspace_root / "browser" / str(run_id)
    folder.mkdir(parents=True)
    (folder / "1.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    shot = await client.get(f"/runs/{run_id}/browser/1.jpg")
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/jpeg"
    for wrong in ("2.jpg", "..%2F..%2Fsecrets.json", "1.png"):
        assert (await client.get(f"/runs/{run_id}/browser/{wrong}")).status_code == 404
    stranger = httpx.AsyncClient(transport=client._transport, base_url="http://test")  # type: ignore[attr-defined]
    assert (await stranger.get(f"/runs/{run_id}/browser/1.jpg")).status_code == 401
    await stranger.aclose()


async def test_files_attached_to_a_new_task_are_granted_and_named_to_it(
    client: httpx.AsyncClient,
) -> None:
    source_id = await attached_document(client, "Price list", "Basic: EUR 5.")
    project = (await client.post("/projects", json={"name": "With files"})).json()
    run = await client.post(
        "/runs",
        json={
            "project_id": project["id"],
            "objective": "Use my price list",
            "attachments": [source_id],
        },
    )
    assert run.status_code == 202 or run.status_code == 201, run.text
    told = (await client.get(f"/runs/{run.json()['id']}/context")).json()
    [part] = [p for p in told["parts"] if p["name"] == "What you attached"]
    assert part["items"] == ["Price list (1 documents)"]
    granted = (await client.get(f"/projects/{project['id']}/sources")).json()
    assert [s["id"] for s in granted] == [source_id]
    missing = await client.post(
        "/runs", json={"project_id": project["id"], "objective": "x", "attachments": [str(uuid4())]}
    )
    assert missing.status_code == 404 and missing.json()["detail"] == "attachment_not_found"


async def test_a_stuck_task_asks_what_stuck_and_shows_in_the_in_tray(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await stuck_run(client, monkeypatch)
    asked = (await client.get(f"/runs/{run_id}/question")).json()
    assert asked["kind"] == "stuck" and asked["step"] == "s3"
    needs = (await client.get("/office/desk")).json()["needs"]
    mine = [n for n in needs if n["run_id"] == run_id]
    assert mine and mine[0]["kind"] == "stuck"
    assert "Case studies" in mine[0]["why"]


async def test_a_hint_goes_to_the_team_and_the_task_carries_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await stuck_run(client, monkeypatch)
    response = await client.post(
        f"/runs/{run_id}/help", json={"action": "hint", "text": "Use the sitemap."}
    )
    assert response.status_code == 202, response.text
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "PENDING"
    controls = (await client.get(f"/runs/{run_id}/controls")).json()
    assert [(c["kind"], c["text"]) for c in controls] == [("instruction", "Use the sitemap.")]
    repository = client.app_state.repository  # type: ignore[attr-defined]
    repository.add_event(UUID(run_id), "run_resumed", {})
    assert (await client.get(f"/runs/{run_id}/question")).json() is None


async def test_skipping_leaves_out_the_step_it_is_stuck_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await stuck_run(client, monkeypatch)
    response = await client.post(f"/runs/{run_id}/help", json={"action": "skip"})
    assert response.status_code == 202
    repository = client.app_state.repository  # type: ignore[attr-defined]
    assert repository.skipped_steps(UUID(run_id)) == {"s3"}


async def test_help_is_only_for_a_paused_task_and_a_hint_needs_words(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await stuck_run(client, monkeypatch)
    empty = await client.post(f"/runs/{run_id}/help", json={"action": "hint", "text": " "})
    assert empty.status_code == 422
    await client.post(f"/runs/{run_id}/help", json={"action": "retry"})
    again = await client.post(f"/runs/{run_id}/help", json={"action": "retry"})
    assert again.status_code == 409


async def test_a_running_step_can_be_skipped_by_name(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await stuck_run(client, monkeypatch)
    skip = await client.post(f"/runs/{run_id}/controls", json={"kind": "skip", "text": "s2"})
    assert skip.status_code == 201
    nameless = await client.post(f"/runs/{run_id}/controls", json={"kind": "skip"})
    assert nameless.status_code == 422
    repository = client.app_state.repository  # type: ignore[attr-defined]
    assert repository.skipped_steps(UUID(run_id)) == {"s2"}


async def test_a_note_sent_mid_step_is_taken_once_and_announced(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await stuck_run(client, monkeypatch)
    repository = client.app_state.repository  # type: ignore[attr-defined]
    await client.post(
        f"/runs/{run_id}/controls", json={"kind": "instruction", "text": "Try /library"}
    )
    assert repository.take_new_instructions(UUID(run_id), "research") == ["Try /library"]
    assert repository.take_new_instructions(UUID(run_id), "research") == []
    events = (await client.get(f"/runs/{run_id}/events")).json()
    heard = [e for e in events if e["kind"] == "instructions_received"]
    assert len(heard) == 1 and heard[0]["payload"]["mid_step"] is True


async def test_a_build_shows_its_files_and_a_preview_link(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await built_run(client, monkeypatch)
    built = (await client.get(f"/runs/{run_id}/build")).json()
    assert [f["path"] for f in built["files"]] == [".gitignore", "app.js", "index.html"]
    assert built["landed"] is True and built["preview"].endswith("/index.html")
    shown = (await client.get(f"/runs/{run_id}/build/file", params={"path": "index.html"})).json()
    assert "Colour" in shown["text"]
    escape = await client.get(f"/runs/{run_id}/build/file", params={"path": "../../etc/passwd"})
    assert escape.status_code == 404
    archive = await client.get(f"/runs/{run_id}/build.zip")
    assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"


async def test_a_preview_is_sandboxed_and_needs_its_private_link(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await built_run(client, monkeypatch)
    link = (await client.get(f"/runs/{run_id}/build")).json()["preview"]
    page = await client.get(link, headers=NO_BEARER)
    assert page.status_code == 200 and "Colour" in page.text
    policy = page.headers["content-security-policy"]
    assert policy.startswith("sandbox allow-scripts") and "allow-same-origin" not in policy
    assert "connect-src 'none'" in policy
    script = await client.get(link.replace("index.html", "app.js"), headers=NO_BEARER)
    assert script.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    forged = link.split("/")
    forged[3] = "0" * 32
    assert (await client.get("/".join(forged), headers=NO_BEARER)).status_code == 404


async def test_a_run_that_built_nothing_has_no_build(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = client.app_state.worker  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "submit", lambda job: None)
    project = (await client.post("/projects", json={"name": "Nothing built"})).json()
    run = (await client.post("/runs", json={"project_id": project["id"], "objective": "Q"})).json()
    assert (await client.get(f"/runs/{run['id']}/build")).status_code == 404


async def test_publishing_needs_the_owners_yes_and_keeps_the_address(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PB1: public by the owner's press; the token sealed; publishing again updates it."""
    run_id = await built_run(client, monkeypatch)
    calls: list[tuple[str, str | None]] = []

    async def netlify(token: str, files: Any, site_id: str | None, http: Any) -> dict[str, Any]:
        calls.append((token, site_id))
        return {"target": "netlify", "site_id": "s1", "url": "https://brave-cat.netlify.app"}

    monkeypatch.setattr("app.hosting.to_netlify", netlify)
    unconfirmed = await client.post(f"/runs/{run_id}/publish", json={"target": "netlify"})
    assert unconfirmed.status_code == 422
    no_token = await client.post(
        f"/runs/{run_id}/publish", json={"target": "netlify", "public": True}
    )
    assert no_token.status_code == 409 and no_token.json()["detail"] == "publish_token_missing"
    first = await client.post(
        f"/runs/{run_id}/publish",
        json={"target": "netlify", "public": True, "token": "nfp_secret_token"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["url"] == "https://brave-cat.netlify.app"
    again = await client.post(f"/runs/{run_id}/publish", json={"target": "netlify", "public": True})
    assert again.status_code == 200
    assert calls == [("nfp_secret_token", None), ("nfp_secret_token", "s1")]
    built = (await client.get(f"/runs/{run_id}/build")).json()
    assert built["published"][0]["url"] == "https://brave-cat.netlify.app"
    assert built["tokens"]["netlify"] is True and "nfp_secret_token" not in json.dumps(built)


async def test_a_task_shows_the_version_it_made_and_every_other(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, one, two = await versioned_run(client, monkeypatch)
    built = (await client.get(f"/runs/{run_id}/build")).json()
    assert built["version"] == 1 and built["commit"] == one[:12]
    assert [v["number"] for v in built["versions"]] == [1, 2]
    assert built["versions"][0]["run_id"] == run_id and built["versions"][1]["latest"]
    shown = await client.get(
        f"/runs/{run_id}/build/file", params={"path": "index.html", "v": one[:12]}
    )
    assert shown.json()["text"] == "<h1>One</h1>", "this task's version, not the latest"
    page = await client.get(built["preview"], headers=NO_BEARER)
    assert page.status_code == 200 and "One" in page.text
    later = await client.get(built["versions"][1]["preview"], headers=NO_BEARER)
    assert "Two" in later.text
    archive = await client.get(f"/runs/{run_id}/build.zip", params={"v": one[:12]})
    assert archive.status_code == 200 and "v1" in archive.headers["content-disposition"]


async def test_restoring_brings_a_version_back_without_losing_any(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, one, two = await versioned_run(client, monkeypatch)
    latest = await client.post(f"/runs/{run_id}/build/restore", json={"commit": two[:12]})
    assert latest.status_code == 409 and latest.json()["detail"] == "already_latest"
    restored = await client.post(f"/runs/{run_id}/build/restore", json={"commit": one[:12]})
    assert restored.status_code == 200 and restored.json() == {"restored": 1, "versions": 3}
    built = (await client.get(f"/runs/{run_id}/build")).json()
    assert [v["subject"] for v in built["versions"]][-1] == "Restore version 1"
    unknown = await client.post(f"/runs/{run_id}/build/restore", json={"commit": "abcdef1"})
    assert unknown.status_code == 409 and unknown.json()["detail"] == "version_not_found"


async def test_a_chosen_version_is_what_gets_published(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, one, two = await versioned_run(client, monkeypatch)
    sent: list[dict[str, bytes]] = []

    async def netlify(token: str, files: Any, site_id: Any, http: Any) -> dict[str, Any]:
        sent.append(dict(files))
        return {"target": "netlify", "site_id": "s1", "url": "https://x.netlify.app"}

    monkeypatch.setattr("app.hosting.to_netlify", netlify)
    done = await client.post(
        f"/runs/{run_id}/publish",
        json={"target": "netlify", "public": True, "token": "nfp_token_x", "version": one[:12]},
    )
    assert done.status_code == 200, done.text
    assert sent[0]["index.html"] == b"<h1>One</h1>"


async def test_the_sidebar_lists_spaces_and_threads_for_the_owner_only(
    client: httpx.AsyncClient,
) -> None:
    """UI-1: one read for the sidebar; like every data route, owner only."""
    project_id = (await client.post("/projects", json={"name": "Sidebar"})).json()["id"]
    await client.post("/runs", json={"project_id": project_id, "objective": "Sidebar check"})

    view = (await client.get("/office/sidebar")).json()

    titles = [t["title"] for space in view["spaces"] for t in space["threads"]]
    assert any("Sidebar" in title for title in titles)
    assert isinstance(view["needs"], int)
    refused = await client.get("/office/sidebar", headers={"Authorization": ""})
    assert refused.status_code == 401


async def test_a_message_queued_for_running_work_waits_and_can_be_changed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI-2b: Enter queues while work runs; the queue is editable until it goes."""
    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    _, run_id = await _running_in_a_thread(client)

    queued = await client.post(f"/runs/{run_id}/queue", json={"text": "Also add a free tier."})
    assert queued.status_code == 201
    control_id = queued.json()["id"]
    edited = await client.put(
        f"/runs/{run_id}/controls/{control_id}", json={"text": "Add a free trial."}
    )
    assert edited.status_code == 200

    controls = (await client.get(f"/runs/{run_id}/controls")).json()
    [mine] = [c for c in controls if c["id"] == control_id]
    assert (mine["kind"], mine["status"], mine["text"]) == ("queued", "QUEUED", "Add a free trial.")

    steered = await client.post(f"/runs/{run_id}/controls/{control_id}/steer")
    assert steered.status_code == 200
    assert steered.json()["kind"] == "instruction"

    again = (await client.post(f"/runs/{run_id}/queue", json={"text": "Never mind."})).json()["id"]
    assert (await client.delete(f"/runs/{run_id}/controls/{again}")).status_code == 204


async def test_a_message_cannot_be_queued_for_work_that_is_not_running(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import Run as RunRow

    monkeypatch.setattr(client.app_state.worker, "submit", lambda job: None)  # type: ignore[attr-defined]
    _, run_id = await _running_in_a_thread(client)
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        row = session.get(RunRow, UUID(run_id))
        assert row is not None
        row.status = "COMPLETED"

    refused = await client.post(f"/runs/{run_id}/queue", json={"text": "Too late."})

    assert refused.status_code == 409
