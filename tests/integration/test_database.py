import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.db.models import Approval, Artifact, Base, Decision, Project, Run, Task
from app.db.session import make_engine


@pytest.mark.integration
def test_migration_round_trip_and_persistence() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    engine = make_engine(database_url)
    assert engine.dialect.name == "postgresql", "TEST_DATABASE_URL must use PostgreSQL"
    schema = f"test_quarterdeck_{uuid4().hex}"
    quoted_schema = engine.dialect.identifier_preparer.quote(schema)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with engine.connect() as connection:
            connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
            connection.execute(text(f"SET search_path TO {quoted_schema}"))
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            context = MigrationContext.configure(connection, opts={"compare_server_default": True})
            assert compare_metadata(context, Base.metadata) == []
            connection.commit()
            with Session(connection) as session, session.begin():
                project = Project(name="Database fixture")
                session.add(project)
                session.flush()
                task = Task(project_id=project.id, type="analysis", objective="Verify storage")
                session.add(task)
                session.flush()
                run = Run(project_id=project.id, root_task_id=task.id, workflow_name="v0")
                session.add(run)
                session.flush()
                artifact = Artifact(
                    project_id=project.id,
                    task_id=task.id,
                    run_id=run.id,
                    type="report",
                    path="fixture/report.json",
                    created_by="chief",
                    metadata_json={"evidence": ["fixture"]},
                )
                approval = Approval(run_id=run.id, task_id=task.id, action_type="modify_code")
                decision = Decision(
                    project_id=project.id,
                    title="Fixture",
                    decision="Proceed",
                    reason="Test",
                    confidence="high",
                    approved_by="owner",
                )
                session.add_all([artifact, approval, decision])
                session.flush()
                project_id, artifact_id, approval_id = project.id, artifact.id, approval.id
                assert task.requires_approval is True
                assert project.created_at.tzinfo is not None
                assert run.state_json == {}
            # A fresh database connection proves the rows were committed, not just cached.
            with engine.connect() as reader:
                reader.execute(text(f"SET search_path TO {quoted_schema}"))
                with Session(reader) as session:
                    saved_project = session.get(Project, project_id)
                    saved_artifact = session.get(Artifact, artifact_id)
                    saved_approval = session.get(Approval, approval_id)
                    assert saved_project is not None and saved_project.name == "Database fixture"
                    assert saved_artifact is not None
                    assert saved_artifact.metadata_json == {"evidence": ["fixture"]}
                    assert saved_approval is not None and saved_approval.status == "PENDING"
            command.downgrade(config, "base")
            assert inspect(connection).get_table_names(schema=schema) == ["alembic_version"]
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        engine.dispose()


@pytest.mark.integration
def test_a_projects_room_becomes_its_first_topic() -> None:
    """0045: each project with a room gets a topic named after it, holding its messages."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    engine = make_engine(database_url)
    schema = f"test_topics_{uuid4().hex}"
    quoted = engine.dialect.identifier_preparer.quote(schema)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with engine.connect() as connection:
            connection.execute(text(f"CREATE SCHEMA {quoted}"))
            connection.execute(text(f"SET search_path TO {quoted}"))
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, "0044")
            talked, quiet = uuid4(), uuid4()
            for project_id, name in ((talked, "Pricing"), (quiet, "Quiet")):
                connection.execute(
                    text("INSERT INTO projects (id, name) VALUES (:id, :name)"),
                    {"id": project_id, "name": name},
                )
            for words in ("First", "Second"):
                connection.execute(
                    text(
                        "INSERT INTO room_messages (project_id, author_kind, author_name, text,"
                        " actions_json) VALUES (:p, 'owner', 'Alex', :t, '[]'::jsonb)"
                    ),
                    {"p": talked, "t": words},
                )
            connection.commit()
            command.upgrade(config, "head")
            connection.commit()
            topics = connection.execute(
                text("SELECT id, title, project_id, home FROM topics")
            ).all()
            assert [(t.title, t.project_id, t.home) for t in topics] == [("Pricing", talked, True)]
            placed = connection.execute(
                text("SELECT count(*) FROM room_messages WHERE topic_id = :t"),
                {"t": topics[0].id},
            ).scalar_one()
            assert placed == 2, "every message is in its project's first topic"
    finally:
        with engine.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
            connection.commit()
        engine.dispose()


@pytest.mark.integration
def test_noted_ideas_move_to_their_own_table_and_back() -> None:
    """0061: ideas leave topic_items with their ids; other items stay."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    engine = make_engine(database_url)
    schema = f"test_ideas_{uuid4().hex}"
    quoted = engine.dialect.identifier_preparer.quote(schema)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with engine.connect() as connection:
            connection.execute(text(f"CREATE SCHEMA {quoted}"))
            connection.execute(text(f"SET search_path TO {quoted}"))
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, "0060")
            topic, idea, decision = uuid4(), uuid4(), uuid4()
            connection.execute(
                text("INSERT INTO topics (id, title) VALUES (:id, 'Pricing')"), {"id": topic}
            )
            for item_id, kind, words in (
                (idea, "idea", "Sell via agencies"),
                (decision, "decision", "Monthly"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO topic_items (id, topic_id, kind, text, status)"
                        " VALUES (:id, :t, :k, :w, 'open')"
                    ),
                    {"id": item_id, "t": topic, "k": kind, "w": words},
                )
            connection.commit()
            command.upgrade(config, "0061")
            connection.commit()
            moved = connection.execute(
                text("SELECT id, text, source, topic_id, status FROM ideas")
            ).all()
            assert [(r.id, r.text, r.source, r.topic_id, r.status) for r in moved] == [
                (idea, "Sell via agencies", "discussion", topic, "open")
            ]
            left = connection.execute(text("SELECT kind FROM topic_items")).scalars().all()
            assert left == ["decision"]
            command.downgrade(config, "0060")
            connection.commit()
            back = (
                connection.execute(text("SELECT id FROM topic_items WHERE kind = 'idea'"))
                .scalars()
                .all()
            )
            assert back == [idea]
    finally:
        with engine.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
            connection.commit()
        engine.dispose()
