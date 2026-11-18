"""What a run leaves behind today, pinned before S4 moves state authority (5a).

The kinds of events a run writes, in order, and the keys of its final state:
the record later parts of Stage 5 (the stream, the side-effect ledger, the
event log as source of truth) must keep or change on purpose.
"""

from typing import Any

import pytest

from app.db.models import Run
from app.lead.run import execute_lead_run
from app.orchestrator.checkpoints import open_checkpointer
from app.orchestrator.dispatcher import execute_run, recover_runs
from app.orchestrator.graph import LEAD_WORKFLOW, AgentSet
from app.orchestrator.state import RunStatus
from app.run_state import rebuild
from tests.integration.test_checkpoints import Breaking
from tests.integration.test_dispatch import context_for, environment, roles  # noqa: F401
from tests.unit.test_graph import critique
from tests.unit.test_lead_run import Model, proposing, said, team

pytestmark = pytest.mark.integration


def record(repository: Any, factory: Any, run_id: Any) -> tuple[list[str], list[str]]:
    kinds = [event["kind"] for event in repository.events(run_id)]
    with factory() as session:
        run = session.get(Run, run_id)
        assert run is not None
        keys = sorted((run.state_json or {}).keys())
        # 5d: the state is what its log of changes adds up to.
        assert rebuild(session, run_id) == run.state_json
    return kinds, keys


TEAM_STEPS = ["plan", "research", "strategy", "critique", "synthesis"]
TEAM_STATE = [
    "approval_id",
    "claim_check",
    "context",
    "convergence",
    "critique",
    "decision",
    "engineering",
    "failed_node",
    "final_report",
    "human_cycles",
    "last_critique",
    "local_evidence",
    "objective",
    "plan",
    "research",
    "research_subtasks",
    "revision_count",
    "scope_digest",
    "source_checks",
    "status",
    "stopped_reason",
    "strategy",
    "synthesis",
    "unreachable_criteria",
    "unverified_claims",
]


def steps(repository: Any, run_id: Any) -> list[str]:
    return [
        str(event["payload"].get("node"))
        for event in repository.events(run_id)
        if event["kind"] == "step_completed"
    ]


async def test_a_team_run_records_each_step_then_finishes(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, _ = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )

    kinds, keys = record(repository, factory, dispatch.run_id)
    assert kinds == ["run_started", *["step_started", "step_completed"] * 5, "run_finished"]
    assert steps(repository, dispatch.run_id) == TEAM_STEPS
    assert keys == TEAM_STATE


async def test_a_lead_run_records_its_steps_and_keeps_a_small_state(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Rain?", workflow=LEAD_WORKFLOW)
    repository.set_context(dispatch.run_id, {"project_name": "Pilot"})
    await execute_lead_run(
        dispatch,
        repository,
        team(Model(proposing(), said("No rain."))),
        "Rain?",
        {"project_name": "Pilot"},
        work=[],
        worker_id="w1",
    )

    kinds, keys = record(repository, factory, dispatch.run_id)
    assert kinds == ["run_started", "lead_step", "lead_step", "team_run_proposed", "run_finished"]
    # finish() replaces the state, keeping what the run was told (5d; it was lost).
    assert keys == ["context", "lead", "lead_todo", "status", "synthesis"]


async def test_a_paused_run_resumes_on_the_same_record(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.add_control(dispatch.run_id, "pause")
    agents, _ = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
        assert state["status"] == RunStatus.PAUSED
        paused, held = record(repository, factory, dispatch.run_id)
        assert paused == [
            "control_queued",
            "run_started",
            "step_started",
            "step_completed",
            "run_paused",
        ]
        # While paused, only what the dispatcher merges as it goes is kept.
        assert held == ["plan"]
        assert repository.requeue(dispatch.run_id)
        await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            3,
            "Assess demand",
            context_for(project_id),
            resume=True,
        )

    kinds, keys = record(repository, factory, dispatch.run_id)
    assert kinds == [
        *paused,
        "run_resumed",
        *["step_started", "step_completed"] * 4,
        "run_finished",
    ]
    assert steps(repository, dispatch.run_id) == TEAM_STEPS
    assert keys == TEAM_STATE


async def test_a_crashed_run_is_recovered_on_the_same_record(environment: Any) -> None:  # noqa: F811
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, made = roles([critique("PASS")])
    breaking = Breaking(made["critic"])
    crashing = AgentSet(
        planner=agents.planner,
        researcher=agents.researcher,
        strategist=agents.strategist,
        critic=breaking,  # type: ignore[arg-type]
        chief=agents.chief,
    )
    async with open_checkpointer(url) as saver:
        with pytest.raises(RuntimeError):
            await execute_run(
                dispatch, repository, crashing, saver, 3, "Assess demand", context_for(project_id)
            )
        crashed, held = record(repository, factory, dispatch.run_id)
        # The process died in the critique: its start is recorded, nothing more.
        assert crashed == [
            "run_started",
            *["step_started", "step_completed"] * 3,
            "step_started",
        ]
        assert held == ["plan"]
        assert recover_runs(repository) == [dispatch.run_id]
        breaking.broken = False
        await execute_run(
            dispatch,
            repository,
            crashing,
            saver,
            3,
            "Assess demand",
            context_for(project_id),
            resume=True,
        )

    kinds, keys = record(repository, factory, dispatch.run_id)
    assert kinds == [
        *crashed,
        "run_recovered",
        "run_resumed",
        *["step_started", "step_completed"] * 2,
        "run_finished",
    ]
    assert steps(repository, dispatch.run_id) == TEAM_STEPS
    assert keys == TEAM_STATE
    assert len(made["planner"].calls) == 1, "what was done is not done again"
