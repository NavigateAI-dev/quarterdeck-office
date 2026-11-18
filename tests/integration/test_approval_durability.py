"""The Phase 4 gate: an approval must survive a full application restart."""

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from app.approvals import ApprovalScope
from app.config import Settings
from app.db.models import Approval, Decision
from app.db.session import make_engine
from app.main import create_app
from app.orchestrator.approval_store import ApprovalConflict, ApprovalStatus, ApprovalStore

pytestmark = pytest.mark.integration

TOKEN = "owner-fixture-token-of-sufficient-length"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    return url


@pytest.fixture
def schema(tmp_path: Path) -> Any:
    url = database_url()
    name = f"test_approval_{uuid4().hex}"
    engine = make_engine(url)
    quoted = engine.dialect.identifier_preparer.quote(name)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {quoted}"))
        connection.execute(text(f"SET search_path TO {quoted}"))
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    engine.dispose()
    settings = Settings(
        _env_file=None,
        database_url=f"{url}?options=-csearch_path%3D{name}",
        owner_api_token=TOKEN,
        owner_name="Alex",
        workspace_root=tmp_path,
    )
    try:
        yield settings
    finally:
        _dispose_factories()
        cleanup = make_engine(url)
        with cleanup.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
            connection.commit()
        cleanup.dispose()


def scope_for(run_id: UUID, project_id: UUID) -> ApprovalScope:
    return ApprovalScope(
        run_id=run_id,
        project_id=project_id,
        objective="Assess demand",
        recommendation="Build a narrow prototype",
        proposed_engineering_scope="Add an export endpoint",
        permitted_paths=["app/export.py"],
    )


async def seed_pending(settings: Settings) -> tuple[str, str]:
    """Create a run and its pending approval in one application instance."""
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=AUTH,
        ) as client,
    ):
        project_id = (await client.post("/projects", json={"name": "P"})).json()["id"]
        run = (await client.post("/runs", json={"project_id": project_id, "objective": "o"})).json()
        store: ApprovalStore = app.state.approvals
        scope = scope_for(UUID(run["id"]), UUID(project_id))
        approval_id = store.open(UUID(run["id"]), UUID(run["root_task_id"]), scope)
    return str(approval_id), scope.digest()


async def test_approval_survives_a_full_application_restart(schema: Settings) -> None:
    approval_id, digest = await seed_pending(schema)

    # A completely new application instance: new engine, worker, and stores.
    app = create_app(schema)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=AUTH,
        ) as client,
    ):
        listed = (await client.get("/approvals/pending", headers=AUTH)).json()
        assert [row["approval"]["id"] for row in listed] == [approval_id]
        # The original scope is still exactly what it was, so it still approves.
        assert listed[0]["brief"]["scope_digest"] == digest
        approved = await client.post(
            f"/approvals/{approval_id}/approve", json={"scope_digest": digest}, headers=AUTH
        )
        assert approved.status_code == 200
        assert approved.json()["resolved_by"] == "Alex"
        assert (await client.get("/approvals/pending", headers=AUTH)).json() == []


async def test_a_resolved_approval_is_preserved_not_deleted(schema: Settings) -> None:
    approval_id, digest = await seed_pending(schema)
    store = ApprovalStore(_factory(schema))
    store.resolve(UUID(approval_id), ApprovalStatus.REJECTED, "Alex", digest)
    with _factory(schema)() as session:
        row = session.get(Approval, UUID(approval_id))
    assert row is not None and row.status == ApprovalStatus.REJECTED
    assert row.scope_json["proposed_engineering_scope"] == "Add an export endpoint"


async def test_replaying_the_approval_node_reuses_the_pending_record(schema: Settings) -> None:
    """Graph replay must not create a second way to authorize the same work."""
    approval_id, _ = await seed_pending(schema)
    factory = _factory(schema)
    store = ApprovalStore(factory)
    with factory() as session:
        approval = session.get(Approval, UUID(approval_id))
        assert approval is not None
        run_id, task_id = approval.run_id, approval.task_id
        project_id = UUID(str(approval.scope_json["project_id"]))

    again = store.open(run_id, task_id, scope_for(run_id, project_id))
    assert str(again) == approval_id
    assert len(store.pending()) == 1


async def test_a_changed_proposal_supersedes_the_old_pending_approval(
    schema: Settings,
) -> None:
    approval_id, _ = await seed_pending(schema)
    factory = _factory(schema)
    store = ApprovalStore(factory)
    with factory() as session:
        approval = session.get(Approval, UUID(approval_id))
        assert approval is not None
        run_id, task_id = approval.run_id, approval.task_id
        project_id = UUID(str(approval.scope_json["project_id"]))

    changed = scope_for(run_id, project_id).model_copy(
        update={"proposed_engineering_scope": "Rewrite billing"}
    )
    new_id = store.open(run_id, task_id, changed)
    assert str(new_id) != approval_id
    with factory() as session:
        superseded = session.get(Approval, UUID(approval_id))
    # The old proposal is preserved for the record but can no longer authorize.
    assert superseded is not None and superseded.status == ApprovalStatus.SUPERSEDED
    assert len(store.pending()) == 1


async def test_a_superseded_approval_cannot_be_approved(schema: Settings) -> None:
    approval_id, digest = await seed_pending(schema)
    factory = _factory(schema)
    store = ApprovalStore(factory)
    with factory() as session:
        approval = session.get(Approval, UUID(approval_id))
        assert approval is not None
        run_id, task_id = approval.run_id, approval.task_id
        project_id = UUID(str(approval.scope_json["project_id"]))
    store.open(
        run_id,
        task_id,
        scope_for(run_id, project_id).model_copy(update={"permitted_paths": ["app/billing.py"]}),
    )
    with pytest.raises(ApprovalConflict, match="already_resolved"):
        store.resolve(UUID(approval_id), ApprovalStatus.APPROVED, "Alex", digest)
    with factory() as session:
        decisions = session.execute(select(func.count()).select_from(Decision)).scalar_one()
    assert decisions == 0


_ENGINES: list[Any] = []


def _factory(settings: Settings) -> Any:
    """One session factory per test, with its engine disposed at teardown."""
    from app.db.session import make_session_factory

    engine = make_engine(settings.database_url.get_secret_value())
    _ENGINES.append(engine)
    return make_session_factory(engine)


def _dispose_factories() -> None:
    while _ENGINES:
        _ENGINES.pop().dispose()


async def test_an_approval_is_carried_out_once_across_a_restart(schema: Settings) -> None:
    """R03: the ledger outlives the process; a second claim never starts the work again."""
    from app.artifacts import ArtifactStore
    from app.db.session import make_session_factory
    from app.orchestrator.gate import DatabaseApprovalGate
    from app.orchestrator.runner import Dispatch

    approval_id, digest = await seed_pending(schema)
    engine = make_engine(schema.database_url.get_secret_value())
    factory = make_session_factory(engine)
    with factory() as session:
        approval = session.get(Approval, UUID(approval_id))
        assert approval is not None
        dispatch = Dispatch(run_id=approval.run_id, task_id=approval.task_id, project_id=uuid4())

    def gate() -> DatabaseApprovalGate:
        # A fresh gate each time, as a restarted process would build.
        return DatabaseApprovalGate(
            dispatch, ApprovalStore(factory), factory, ArtifactStore(schema.workspace_root)
        )

    assert gate().begin_execution(approval_id, digest) == ("new", None)
    assert gate().begin_execution(approval_id, digest) == ("in_flight", None)
    gate().finish_execution(approval_id, {"status": "completed", "pull_request_url": "u"})
    held, report = gate().begin_execution(approval_id, digest)
    assert held == "finished" and report == {"status": "completed", "pull_request_url": "u"}
    engine.dispose()
