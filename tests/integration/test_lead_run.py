"""A lead task against the real run repository and database."""

from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Artifact, Run
from app.lead.run import execute_lead_run
from app.orchestrator.dispatcher import RunNotClaimed
from app.orchestrator.graph import LEAD_WORKFLOW
from app.orchestrator.state import RunStatus
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture
from tests.unit.test_lead_run import Model, proposing, said, team

pytestmark = pytest.mark.integration


async def test_a_lead_task_is_claimed_answered_and_recorded(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Rain in Lyon?", workflow=LEAD_WORKFLOW)

    state = await execute_lead_run(
        dispatch,
        repository,
        team(Model(proposing(), said("No rain today.\n\nCheck again at noon."))),
        "Rain in Lyon?",
        {"project_name": "Pilot"},
        work=[],
        worker_id="w1",
    )

    assert state["status"] == RunStatus.COMPLETED
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None
        assert run.workflow_name == LEAD_WORKFLOW
        assert run.status == RunStatus.COMPLETED
        assert run.state_json["synthesis"]["lead"] == "No rain today."
        assert run.state_json["lead"]["calls"] == 2
        kinds = [
            row.type
            for row in session.scalars(select(Artifact).where(Artifact.run_id == dispatch.run_id))
        ]
    assert kinds == ["synthesis"]
    events = [e["kind"] for e in repository.events(dispatch.run_id)]
    assert events[0] == "run_started" and events[-1] == "run_finished"
    assert "team_run_proposed" in events and events.count("lead_step") == 2


async def test_a_second_worker_cannot_run_the_same_lead_task(environment: Any) -> None:  # noqa: F811
    repository, _, project_id, _ = environment
    dispatch = repository.create(project_id, "Q", workflow=LEAD_WORKFLOW)
    await execute_lead_run(
        dispatch, repository, team(Model(said("A."))), "Q", {}, work=[], worker_id="w1"
    )

    with pytest.raises(RunNotClaimed):
        await execute_lead_run(
            dispatch, repository, team(Model(said("B."))), "Q", {}, work=[], worker_id="w2"
        )


def test_a_task_box_request_goes_to_the_lead_unless_it_needs_the_team(
    environment: Any,  # noqa: F811
) -> None:
    from app.db.models import OfficeSettings, Project
    from app.orchestrator.graph import ANALYSIS_WORKFLOW
    from app.run_start import answering_workflow
    from tests.unit.test_direct_completion import settings

    _, factory, project_id, _ = environment
    config = settings()
    with factory() as session:
        assert answering_workflow(session, config, project_id, None, False) == LEAD_WORKFLOW
        slides = {"shape": "slides", "format": "pptx"}
        assert answering_workflow(session, config, project_id, slides, False) == ANALYSIS_WORKFLOW
        assert answering_workflow(session, config, project_id, None, True) == ANALYSIS_WORKFLOW
    with factory() as session, session.begin():
        project = session.get(Project, project_id)
        assert project is not None
        project.deliverable_json = {"shape": "slides", "format": "pptx"}
    with factory() as session:
        assert answering_workflow(session, config, project_id, None, False) == ANALYSIS_WORKFLOW
    with factory() as session, session.begin():
        session.add(OfficeSettings(only=True))
        project = session.get(Project, project_id)
        assert project is not None
        project.deliverable_json = {}
    with factory() as session:
        # No office setting sends it to the team any more (27 Oct): only its needs do.
        assert answering_workflow(session, config, project_id, None, False) == LEAD_WORKFLOW


async def test_an_instruction_reaches_a_lead_task_and_is_marked_received(
    environment: Any,  # noqa: F811
) -> None:
    from typing import Any as _Any

    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Pilot tiers", workflow=LEAD_WORKFLOW)
    repository.add_control(dispatch.run_id, "instruction", "Skip retail.", None)
    seen: list[list[dict[str, _Any]]] = []

    class Seeing(Model):
        async def complete(
            self, model_alias: str, messages: list[dict[str, _Any]], **options: _Any
        ) -> _Any:
            seen.append(list(messages))
            return await super().complete(model_alias, messages, **options)

    await execute_lead_run(
        dispatch,
        repository,
        team(Seeing(said("Done."))),
        "Pilot tiers",
        {},
        work=[],
        worker_id="w1",
    )

    assert seen[0][-1]["content"] == "The owner, while you work: Skip retail."
    kinds = [e["kind"] for e in repository.events(dispatch.run_id)]
    assert "instructions_received" in kinds


async def test_a_cancelled_lead_task_stops_and_says_so(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Pilot tiers", workflow=LEAD_WORKFLOW)
    repository.add_control(dispatch.run_id, "cancel", None, None)

    state = await execute_lead_run(
        dispatch, repository, team(Model(said("never"))), "Pilot tiers", {}, work=[], worker_id="w1"
    )

    assert state["status"] == RunStatus.CANCELLED
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None and run.status == RunStatus.CANCELLED


async def test_the_lead_step_list_shows_on_the_thread_work_card(environment: Any) -> None:  # noqa: F811
    import json as _json

    from app.gateway.client import Completion, ToolCall, Usage
    from app.handoff import work_view

    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Pilot tiers", workflow=LEAD_WORKFLOW)
    items = {
        "items": [
            {"text": "Find prices", "status": "done"},
            {"text": "Write tiers", "status": "in_progress"},
        ]
    }
    planning = Completion(
        model_alias="m",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[
            ToolCall(id="c1", name="todo", arguments=items, raw_arguments=_json.dumps(items))
        ],
    )

    await execute_lead_run(
        dispatch,
        repository,
        team(Model(planning, said("Done."))),
        "Pilot tiers",
        {},
        work=[],
        worker_id="w1",
    )

    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None
        view = work_view(session, run, {})
    assert (view["steps_done"], view["steps"], view["doing"]) == (1, 2, "Write tiers")
    # The dock's Board lists the steps themselves.
    assert [item["status"] for item in view["todo"]] == ["done", "in_progress"]
