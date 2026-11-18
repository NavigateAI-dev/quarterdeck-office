"""Sample mode: seen before any key, and never mistaken for a live test."""

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.artifacts import ArtifactStore
from app.db.models import Artifact, Project, Run, UsageEvent
from app.db.session import make_engine
from app.sample_office import LABEL, NOT_REAL, install, installed, is_sample, remove

pytestmark = pytest.mark.integration


@pytest.fixture
def session_and_store(tmp_path: Path) -> Any:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    schema = f"test_sample_{uuid4().hex}"
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

    scoped = make_engine(f"{url}?options=-csearch_path%3D{schema}")
    factory = sessionmaker(bind=scoped)
    store = ArtifactStore(tmp_path / "workspace")
    with factory() as session:
        yield session, store
    scoped.dispose()
    engine = make_engine(url)
    with engine.connect() as connection:
        connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
        connection.commit()
    engine.dispose()


def test_the_sample_says_everywhere_that_it_is_not_real_work(session_and_store: Any) -> None:
    session, store = session_and_store
    made = install(session, store)
    assert made["created"] is True

    project = installed(session)
    assert project is not None and NOT_REAL in project.description

    run = session.scalar(select(Run).where(Run.project_id == project.id))
    assert run is not None and is_sample(run), "the run must be labelled as sample work"
    assert LABEL in run.labels_json

    artifact = session.scalar(select(Artifact).where(Artifact.project_id == project.id))
    assert artifact is not None and is_sample(artifact)
    assert artifact.created_by == LABEL
    # And the work itself says so, so it cannot be read as a live result.
    body = store.read(project.id, artifact.id, 1)
    assert NOT_REAL in body


def test_the_sample_spends_nothing_and_calls_nothing(session_and_store: Any) -> None:
    session, store = session_and_store
    install(session, store)
    assert session.scalars(select(UsageEvent)).all() == [], "a sample must record no usage"
    run = session.scalar(select(Run))
    assert run is not None
    assert run.budget_json.get("spent_usd") == 0


def test_installing_the_sample_twice_makes_one(session_and_store: Any) -> None:
    """A new owner may well press it again to find where it went."""
    session, store = session_and_store
    first = install(session, store)
    second = install(session, store)
    assert second["created"] is False
    assert second["project_id"] == first["project_id"]
    assert len(session.scalars(select(Project)).all()) == 1


def test_removing_the_sample_takes_everything_with_it(session_and_store: Any) -> None:
    session, store = session_and_store
    made = install(session, store)
    # A run's state now has a log (5d); it goes with the run.
    from uuid import UUID

    from app import run_state

    sample = session.get(Run, UUID(made["run_id"]))
    run_state.write(session, sample, run_state.SET, {"plan": {}}, "test")
    session.flush()
    assert remove(session, store) is True
    assert installed(session) is None
    assert session.scalars(select(Run)).all() == []
    assert session.scalars(select(Artifact)).all() == []
    # Removing what is not there is not an error.
    assert remove(session, store) is False
