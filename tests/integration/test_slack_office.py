"""SlackOffice's own glue, before S3 folds the chat paths into one ChatService.

Characterisation tests: who may speak for a workspace, projects
opened from Slack, duplicate event delivery, and the offer to start work.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.channels.inbound_office import SlackOffice
from app.channels.members import MEMBER
from app.config import Settings
from app.db.models import ChannelMember, Project
from app.db.session import make_engine, make_session_factory

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    name = f"test_slack_office_{uuid4().hex}"
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
    scoped = make_engine(f"{url}?options=-csearch_path%3D{name}")
    try:
        yield make_session_factory(scoped)
    finally:
        scoped.dispose()
        cleanup = make_engine(url)
        with cleanup.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
            connection.commit()
        cleanup.dispose()


def office(factory: sessionmaker[Session], tmp_path: Path) -> SlackOffice:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused@localhost/unused",
        owner_api_token="owner-fixture-token-of-sufficient-length",
        owner_name="Alex",
        workspace_root=tmp_path,
    )
    unused: Any = object()
    return SlackOffice(factory, unused, unused, unused, settings, app_id=uuid4())


def test_one_owner_per_workspace_and_members_kept_apart(factory: Any, tmp_path: Path) -> None:
    slack = office(factory, tmp_path)
    assert slack.owner_of("T1") is None

    slack.set_owner("T1", "U-owner")
    with factory() as session, session.begin():
        session.add(
            ChannelMember(kind="slack", workspace_id="T1", external_user_id="U-m", role=MEMBER)
        )

    assert slack.owner_of("T1") == "U-owner"
    assert slack.role_of("T1", "U-owner") == "owner"
    assert slack.role_of("T1", "U-m") == MEMBER
    assert slack.role_of("T1", "U-stranger") is None
    assert slack.owner_of("T2") is None

    slack.set_owner("T1", "U-new")
    assert slack.owner_of("T1") == "U-new"
    assert slack.role_of("T1", "U-owner") is None


def test_open_project_creates_once_and_reactivates(factory: Any, tmp_path: Path) -> None:
    slack = office(factory, tmp_path)

    first = slack.open_project("Pilot")
    with factory() as session, session.begin():
        session.get(Project, first).status = "ARCHIVED"  # type: ignore[union-attr]
    second = slack.open_project("Pilot")

    assert first == second
    with factory() as session:
        rows = list(session.scalars(select(Project).where(Project.name == "Pilot")))
    assert len(rows) == 1
    assert rows[0].status == "ACTIVE"
    assert rows[0].description == "Created from Slack."


def test_a_redelivered_event_is_handled_once(factory: Any, tmp_path: Path) -> None:
    slack = office(factory, tmp_path)

    assert slack.first_time("Ev1") is True
    assert slack.first_time("Ev1") is False
    assert slack.first_time("") is False


def test_the_office_bot_offers_the_full_analysis(factory: Any, tmp_path: Path) -> None:
    slack = office(factory, tmp_path)

    assert slack.work_offer("Pilot") == "I can have the office run the full analysis in Pilot:"


async def test_a_slack_question_is_answered_by_the_lead_with_a_lookup(
    factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_lead_service import Model, call, said

    bot = office(factory, tmp_path)
    model = Model(
        call("search", {"query": "rain Lyon"}),
        said("Rain from 15:00 (https://www.knmi.nl/weer)."),
    )

    async def search(query: str) -> list[Any]:
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                title="KNMI", url="https://www.knmi.nl/weer", snippet="rain 15:00", text=""
            )
        ]

    async def status() -> str:
        return "All quiet."

    monkeypatch.setattr(bot, "_completer", lambda agent: (model, "chief"))
    monkeypatch.setattr(bot, "_lookup_tools", lambda: (search, None, None))
    monkeypatch.setattr(bot, "status_line", status)
    monkeypatch.setattr(bot, "facts", lambda agent=None: {"office": "fixture"})

    said_back = await bot.converse([], "Will it rain in Lyon today?")

    assert said_back is not None and not isinstance(said_back, str)
    assert said_back.text == "Rain from 15:00 (https://www.knmi.nl/weer)."
    assert said_back.task is None and said_back.watch is None


async def test_the_lead_in_slack_offers_large_work_as_a_task(
    factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_lead_service import Model, call, said

    bot = office(factory, tmp_path)
    model = Model(
        call("propose_team_run", {"objective": "NL e-bike market study", "why": "Many sources"}),
        said("A first view: the market grew in 2025. I proposed a full study."),
    )

    async def status() -> str:
        return "All quiet."

    monkeypatch.setattr(bot, "_completer", lambda agent: (model, "chief"))
    monkeypatch.setattr(bot, "_lookup_tools", lambda: (None, None, None))
    monkeypatch.setattr(bot, "status_line", status)
    monkeypatch.setattr(bot, "facts", lambda agent=None: {"office": "fixture"})

    said_back = await bot.converse([], "Do a full market study of Dutch e-bikes.")

    assert not isinstance(said_back, str) and said_back is not None
    assert said_back.task == "NL e-bike market study"
    assert said_back.text.startswith("A first view")


async def test_the_calls_behind_a_slack_answer_are_kept_against_its_thread(
    factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import ModelCall
    from tests.unit.test_lead_service import Model, said

    bot = office(factory, tmp_path)
    model = Model(said("No rain today."))

    async def status() -> str:
        return "All quiet."

    monkeypatch.setattr(bot, "_completer", lambda agent: (model, "chief"))
    monkeypatch.setattr(bot, "_lookup_tools", lambda: (None, None, None))
    monkeypatch.setattr(bot, "status_line", status)
    monkeypatch.setattr(bot, "facts", lambda agent=None: {"office": "fixture"})

    await bot.converse([], "Will it rain?", chat_ref="slack:C1:171.5")

    with factory() as session:
        kept = list(session.scalars(select(ModelCall)))
    assert [(c.origin, c.chat_ref, c.run_id, c.status) for c in kept] == [
        ("slack", "slack:C1:171.5", None, "ok")
    ]
