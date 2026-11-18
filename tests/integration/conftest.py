"""Fixtures for the API tests: a running office on its own schema (split from test_api.py)."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import Settings
from app.db.session import make_engine
from app.main import create_app
from app.secret_store import generate_root_key
from tests.integration.api_support import (
    FakeIndexer,
    FakeSlack,
    database_url,
)


@pytest.fixture
async def client(tmp_path: Path) -> Any:
    url = database_url()
    schema = f"test_api_{uuid4().hex}"
    engine = make_engine(url)
    quoted = engine.dialect.identifier_preparer.quote(schema)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {quoted}"))
        connection.execute(text(f"SET search_path TO {quoted}"))
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    engine.dispose()

    generate_root_key(tmp_path / "root.key")
    settings = Settings(
        _env_file=None,
        database_url=f"{url}?options=-csearch_path%3D{schema}",
        owner_api_token="owner-fixture-token-of-sufficient-length",
        workspace_root=tmp_path,
        secret_key_file=tmp_path / "root.key",
        # Not the office's own backups folder: the status view reads it, and a
        # real archive there would make a test depend on when it last ran.
        backup_dir=tmp_path / "backups",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer owner-fixture-token-of-sufficient-length"},
        ) as http_client,
    ):
        # Exposes the running worker to tests that check its execution wiring.
        http_client.app_state = app.state  # type: ignore[attr-defined]
        # Lets a test start a second office on the same data, as a restart would.
        http_client.settings = settings  # type: ignore[attr-defined]
        yield http_client
    cleanup = make_engine(url)
    with cleanup.connect() as connection:
        connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
        connection.commit()
    cleanup.dispose()


@pytest.fixture
def indexer(monkeypatch: pytest.MonkeyPatch) -> FakeIndexer:
    fake = FakeIndexer()
    monkeypatch.setattr("app.api.routers.reach.build_indexer", lambda settings: fake)
    return fake


@pytest.fixture
def no_dispatch(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Keep the worker from executing runs created by these tests."""
    worker = client.app_state.worker  # type: ignore[attr-defined]
    submitted: list[Any] = []
    monkeypatch.setattr(worker, "submit", submitted.append)
    return submitted


@pytest.fixture
def fake_slack(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    fake = FakeSlack()
    monkeypatch.setattr("app.api.channel_routes.adapter_for", lambda kind: fake)
    monkeypatch.setattr("app.channels.notifications.adapter_for", lambda kind: fake)
    monkeypatch.setattr("app.channels.progress.adapter_for", lambda kind: fake)
    monkeypatch.setattr("app.channels.inbound_office.adapter_for", lambda kind: fake)
    return fake
