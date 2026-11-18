"""A run's state is what its log of changes adds up to."""

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text, update

from app.db.models import Run
from app.orchestrator.checkpoints import open_checkpointer
from app.orchestrator.dispatcher import execute_run
from app.orchestrator.review import ReviewStore
from app.orchestrator.state import RunStatus
from app.run_state import changes, rebuild
from tests.integration.test_dispatch import context_for, environment, roles  # noqa: F401
from tests.integration.test_live_notify import schema_of
from tests.unit.test_graph import critique

pytestmark = pytest.mark.integration


async def test_a_team_runs_log_says_who_changed_its_state(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.set_context(dispatch.run_id, context_for(project_id))
    agents, _ = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )

    with factory() as session:
        log = changes(session, dispatch.run_id)
        run = session.get(Run, dispatch.run_id)
        assert run is not None
        assert rebuild(session, dispatch.run_id) == run.state_json
    sources = [change["source"] for change in log]
    assert sources[0] == "context" and sources[-1] == "finish"
    assert set(sources[1:-1]) == {"dispatch"}, "what the dispatcher merges as it goes"
    assert [change["op"] for change in log][-1] == "replace"


async def test_a_restart_from_review_is_a_change_in_the_log(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.set_context(dispatch.run_id, {"project_name": "Pilot"})
    repository.merge_state(dispatch.run_id, {"plan": {"steps": []}, "human_cycles": 1})
    with factory() as session, session.begin():
        session.execute(
            update(Run).where(Run.id == dispatch.run_id).values(status=RunStatus.NEEDS_HUMAN_REVIEW)
        )

    ReviewStore(factory).restart(dispatch.run_id, "Alex")

    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None
        assert run.state_json == {
            "context": {"project_name": "Pilot"},
            "human_cycles": 2,
            "reviewed_by": "Alex",
        }
        assert rebuild(session, dispatch.run_id) == run.state_json
        assert changes(session, dispatch.run_id)[-1]["source"] == "review"


async def test_a_run_from_before_the_log_starts_it_with_its_state(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    schema = schema_of(factory)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    engine = factory.kw["bind"]

    def migrate(to: str, step: Any) -> None:
        with engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config.attributes["connection"] = connection
            step(config, to)
            connection.commit()

    migrate("0070", command.downgrade)
    dispatch = repository.create(project_id, "Assess demand")
    with factory() as session, session.begin():
        # Written as the office did before 0071, straight onto the row.
        session.execute(
            update(Run).where(Run.id == dispatch.run_id).values(state_json={"plan": {"x": 1}})
        )
    migrate("0071", command.upgrade)

    with factory() as session:
        assert rebuild(session, dispatch.run_id) == {"plan": {"x": 1}}
        assert [c["source"] for c in changes(session, dispatch.run_id)] == ["baseline"]


async def test_one_reading_of_what_a_run_waits_on_for_runner_and_thread(environment: Any) -> None:  # noqa: F811
    from app.rooms import _budget_ask
    from app.run_asks import BUDGET_ASK, STUCK_ASK, pending_ask

    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.add_event(dispatch.run_id, "budget_ask", {"limit": "cost"})
    assert repository.pending_budget_ask(dispatch.run_id) == {"limit": "cost"}

    # The owner chose to finish with what there is: that answers it, for both.
    repository.add_event(dispatch.run_id, "finish_requested", {})
    with factory() as session:
        assert pending_ask(session, dispatch.run_id, BUDGET_ASK) is None
        assert _budget_ask(session, dispatch.run_id) is None, "the thread no longer asks"
    assert repository.pending_budget_ask(dispatch.run_id) is None

    repository.add_event(dispatch.run_id, "stuck_ask", {"what": "a page will not open"})
    assert repository.pending_stuck_ask(dispatch.run_id) == {"what": "a page will not open"}
    repository.add_event(dispatch.run_id, "run_resumed", {})
    with factory() as session:
        assert pending_ask(session, dispatch.run_id, STUCK_ASK) is None
