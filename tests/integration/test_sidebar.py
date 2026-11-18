"""The sidebar: spaces, their threads and each thread's state."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db.models import Project, Run, Task, Topic, Venture
from app.sidebar import IDLE_SHOWN, NO_SPACE, sidebar
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


def _space(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(space for space in view["spaces"] if space["name"] == name)


def test_threads_sit_in_their_space_with_one_state_each(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    with factory() as session, session.begin():
        venture = Venture(name="OPC")
        session.add(venture)
        session.flush()
        project = session.get(Project, project_id)
        assert project is not None
        project.venture_id = venture.id
        talk = Topic(title="Pricing for pilot", project_id=project_id, venture_id=venture.id)
        session.add(talk)
        session.flush()
        talk_id = talk.id
    waiting = repository.create(project_id, "Lead reactivation report")
    working = repository.create(project_id, "Agent runtime fit")
    done = repository.create(project_id, "LangGraph vs CrewAI")
    with factory() as session, session.begin():
        session.get(Run, waiting.run_id).status = "WAITING_FOR_APPROVAL"  # type: ignore[union-attr]
        session.get(Run, working.run_id).status = "RUNNING"  # type: ignore[union-attr]
        finished = session.get(Run, done.run_id)
        assert finished is not None
        finished.status = "COMPLETED"
        finished.completed_at = NOW - timedelta(hours=2)
        for dispatch in (waiting, working, done):
            session.get(Task, dispatch.task_id).created_at = NOW - timedelta(hours=3)  # type: ignore[union-attr]

    with factory() as session:
        view = sidebar(session, NOW)

    opc = _space(view, "OPC")
    states = {t["title"]: (t["state"], t["badge"]) for t in opc["threads"]}
    assert states["Lead reactivation report"] == ("needs", "Approval")
    assert states["Agent runtime fit"] == ("working", None)
    assert states["LangGraph vs CrewAI"] == ("done", None)
    assert states["Pricing for pilot"] == ("idle", None)
    assert [t["state"] for t in opc["threads"]][:2] == ["needs", "working"]
    assert view["needs"] == 1
    talk = next(t for t in opc["threads"] if t["kind"] == "topic")
    assert talk["href"] == f"#topic/{talk_id}"


def test_a_project_home_discussion_shows_its_tasks_instead_of_listing_them(
    environment: Any,  # noqa: F811
) -> None:
    repository, factory, project_id, _ = environment
    with factory() as session, session.begin():
        session.add(Topic(title="P home", project_id=project_id, home=True))
    started = repository.create(project_id, "A task asked from the task box")
    with factory() as session, session.begin():
        session.get(Run, started.run_id).status = "RUNNING"  # type: ignore[union-attr]
        session.get(Task, started.task_id).created_at = NOW  # type: ignore[union-attr]

    with factory() as session:
        threads = _space(sidebar(session, NOW), NO_SPACE)["threads"]

    assert [(t["kind"], t["title"], t["state"]) for t in threads] == [
        ("topic", "P home", "working")
    ]


def test_a_space_shows_five_idle_threads_and_counts_the_rest(environment: Any) -> None:  # noqa: F811
    _, factory, _, _ = environment
    with factory() as session, session.begin():
        for n in range(IDLE_SHOWN + 3):
            session.add(Topic(title=f"Talk {n}", last_activity_at=NOW - timedelta(days=n)))

    with factory() as session:
        space = _space(sidebar(session, NOW), NO_SPACE)

    assert [t["title"] for t in space["threads"]] == [f"Talk {n}" for n in range(IDLE_SHOWN)]
    assert space["more"] == 3
