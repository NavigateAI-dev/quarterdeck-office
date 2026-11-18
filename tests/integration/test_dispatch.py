import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text

from app.artifacts import ArtifactStore
from app.db.models import Artifact, Project, Run
from app.db.session import make_engine, make_session_factory
from app.orchestrator.checkpoints import open_checkpointer
from app.orchestrator.dispatcher import RunNotClaimed, execute_run, recover_runs
from app.orchestrator.graph import AgentSet, RunFailure
from app.orchestrator.runner import RunRepository, UnknownProject, artifact_id_for
from app.orchestrator.state import RunStatus
from app.schemas import Critique, ResearchReport, StrategyProposal, Synthesis, TaskPlan
from tests.unit.test_graph import PLAN, RESEARCH, STRATEGY, SYNTHESIS, Role, critique

pytestmark = pytest.mark.integration


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    return url


@pytest.fixture
def environment(tmp_path: Path) -> Any:
    url = database_url()
    engine = make_engine(url)
    schema = f"test_dispatch_{uuid4().hex}"
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
    scoped = make_engine(url).execution_options(schema_translate_map={None: schema})
    factory = make_session_factory(scoped)
    repository = RunRepository(factory, ArtifactStore(tmp_path))
    with factory() as session, session.begin():
        project = Project(name="Dispatch fixture")
        session.add(project)
        session.flush()
        project_id = project.id
    try:
        yield repository, factory, project_id, url
    finally:
        scoped.dispose()
        cleanup = make_engine(url)
        with cleanup.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {quoted} CASCADE"))
            connection.commit()
        cleanup.dispose()


def roles(critiques: list[dict[str, Any]]) -> tuple[AgentSet, dict[str, Role]]:
    made = {
        "planner": Role("planner", TaskPlan, [PLAN]),
        "researcher": Role("researcher", ResearchReport, [RESEARCH]),
        "strategist": Role("strategist", StrategyProposal, [STRATEGY]),
        "critic": Role("critic", Critique, critiques),
        "chief": Role("chief", Synthesis, [SYNTHESIS]),
    }
    return AgentSet(**made), made  # type: ignore[arg-type]


def context_for(project_id: Any) -> dict[str, Any]:
    return {
        "organization": "Northwind",
        "project_id": str(project_id),
        "project_name": "Harbor AI",
    }


async def test_run_persists_every_artifact_and_completes(environment: Any) -> None:
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, _ = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    assert state["status"] == RunStatus.COMPLETED
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None and run.status == RunStatus.COMPLETED
        assert run.completed_at is not None
        types = sorted(
            row[0]
            for row in session.execute(
                select(Artifact.type).where(Artifact.run_id == dispatch.run_id)
            )
        )
    assert types == ["critique", "research_report", "strategy_proposal", "synthesis", "task_plan"]


async def test_a_claimed_run_cannot_be_dispatched_twice(environment: Any) -> None:
    repository, _, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, made = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
        with pytest.raises(RunNotClaimed):
            await execute_run(
                dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
            )
    assert len(made["planner"].calls) == 1


async def test_repeated_stage_recording_does_not_duplicate_artifacts(environment: Any) -> None:
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    first = repository.record_stage(dispatch, "research", RESEARCH)
    second = repository.record_stage(dispatch, "research", RESEARCH)
    assert first == artifact_id_for(dispatch.run_id, "research", 1)
    assert second == artifact_id_for(dispatch.run_id, "research", 2)
    with factory() as session:
        count = session.execute(
            select(func.count()).select_from(Artifact).where(Artifact.run_id == dispatch.run_id)
        ).scalar_one()
    assert count == 2


async def test_replaying_the_same_version_reconciles_instead_of_duplicating(
    environment: Any,
) -> None:
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    artifact_id = repository.record_stage(dispatch, "plan", PLAN)
    with factory() as session, session.begin():
        session.execute(delete(Artifact).where(Artifact.id == artifact_id))
    # The file already exists; recording again must reuse it, not crash.
    again = repository.record_stage(dispatch, "plan", PLAN)
    assert again == artifact_id


async def test_failure_records_the_node_and_does_not_complete(environment: Any) -> None:
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")

    class Broken:
        role = "strategist"

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.agents.base import AgentOutputError

            raise AgentOutputError("strategist", "malformed_structured_output")

    agents, _ = roles([critique("PASS")])
    broken = AgentSet(
        planner=agents.planner,
        researcher=agents.researcher,
        strategist=Broken(),  # type: ignore[arg-type]
        critic=agents.critic,
        chief=agents.chief,
    )
    async with open_checkpointer(url) as saver:
        with pytest.raises(RunFailure):
            await execute_run(
                dispatch, repository, broken, saver, 3, "Assess demand", context_for(project_id)
            )
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.error == "strategist:malformed_structured_output"


async def test_unknown_project_is_rejected(environment: Any) -> None:
    repository, _, _, _ = environment
    with pytest.raises(UnknownProject):
        repository.create(uuid4(), "Assess demand")


async def test_interrupted_runs_are_reclaimed_for_recovery(environment: Any) -> None:
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    assert repository.claim(dispatch.run_id)
    assert repository.active_run_ids() == [dispatch.run_id]
    assert recover_runs(repository) == [dispatch.run_id]
    # A reclaimed run is eligible to be claimed again by the recovering process.
    assert repository.claim(dispatch.run_id)


async def test_completed_stages_persist_even_when_a_later_stage_fails(environment: Any) -> None:
    """A failure must not discard artifacts already produced by earlier nodes."""
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")

    class Broken:
        role = "critic"

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.agents.base import AgentOutputError

            raise AgentOutputError("critic", "provider_rejected_request")

    agents, _ = roles([critique("PASS")])
    broken = AgentSet(
        planner=agents.planner,
        researcher=agents.researcher,
        strategist=agents.strategist,
        critic=Broken(),  # type: ignore[arg-type]
        chief=agents.chief,
    )
    async with open_checkpointer(url) as saver:
        with pytest.raises(RunFailure):
            await execute_run(
                dispatch, repository, broken, saver, 3, "Assess demand", context_for(project_id)
            )
    with factory() as session:
        types = sorted(
            row[0]
            for row in session.execute(
                select(Artifact.type).where(Artifact.run_id == dispatch.run_id)
            )
        )
        run = session.get(Run, dispatch.run_id)
    assert types == ["research_report", "strategy_proposal", "task_plan"]
    assert run is not None and run.status == RunStatus.FAILED


async def test_a_queued_run_is_recovered_not_orphaned(environment: Any) -> None:
    """The work queue is in memory, so a PENDING row with no job is lost."""
    repository, factory, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
    assert run is not None and run.status == "PENDING"
    assert repository.mark_for_recovery() == [dispatch.run_id]
    assert repository.claim(dispatch.run_id) is True


async def test_a_run_that_never_started_is_not_resumed(environment: Any) -> None:
    """Resuming without a checkpoint would invoke the graph with no state."""
    from app.orchestrator.approval_store import ApprovalStore
    from app.runtime.worker import recovery_jobs

    repository, factory, project_id, _ = environment
    approvals = ApprovalStore(factory)
    dispatch = repository.create(project_id, "Assess demand")
    fresh = recovery_jobs(repository, approvals, [dispatch.run_id])
    assert fresh and fresh[0].resume is False
    assert fresh[0].resume_node is None

    repository.claim(dispatch.run_id)
    started = recovery_jobs(repository, approvals, [dispatch.run_id])
    assert started and started[0].resume is True


async def test_a_decided_approval_re_enters_at_the_approval_node(
    environment: Any,
) -> None:
    """The graph already reached its end, so resuming alone would do nothing."""
    from app.approvals import ApprovalScope
    from app.orchestrator.approval_store import ApprovalStatus, ApprovalStore
    from app.runtime.worker import recovery_jobs

    repository, factory, project_id, _ = environment
    approvals = ApprovalStore(factory)
    dispatch = repository.create(project_id, "Assess demand")
    repository.claim(dispatch.run_id)
    scope = ApprovalScope(
        run_id=dispatch.run_id,
        project_id=project_id,
        objective="o",
        recommendation="r",
        proposed_engineering_scope="s",
    )
    approval_id = approvals.open(dispatch.run_id, dispatch.task_id, scope)

    waiting = recovery_jobs(repository, approvals, [dispatch.run_id])
    assert waiting[0].resume_node is None

    approvals.resolve(approval_id, ApprovalStatus.APPROVED, "Alex", scope.digest())
    decided = recovery_jobs(repository, approvals, [dispatch.run_id])
    assert decided[0].resume_node == "approval"
    assert decided[0].resume is True


async def test_a_pause_stops_at_the_next_step_and_resume_continues(environment: Any) -> None:
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.add_control(dispatch.run_id, "pause")
    agents, made = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
        assert state["status"] == RunStatus.PAUSED
        with factory() as session:
            assert session.get(Run, dispatch.run_id).status == RunStatus.PAUSED  # type: ignore[union-attr]
        assert made["researcher"].calls == []
        [control] = repository.controls(dispatch.run_id)
        assert control["status"] == "APPLIED"

        assert repository.requeue(dispatch.run_id)
        state = await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            3,
            "Assess demand",
            context_for(project_id),
            resume=True,
        )
    assert state["status"] == RunStatus.COMPLETED
    assert len(made["planner"].calls) == 1  # resumed, not restarted


async def test_a_cancel_stops_and_keeps_what_was_produced(environment: Any) -> None:
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.add_control(dispatch.run_id, "cancel")
    agents, made = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    assert state["status"] == RunStatus.CANCELLED
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        assert run is not None and run.status == RunStatus.CANCELLED
        kinds = [
            row[0]
            for row in session.execute(
                select(Artifact.type).where(Artifact.run_id == dispatch.run_id)
            )
        ]
    assert kinds == ["task_plan"]
    assert not repository.requeue(dispatch.run_id)  # cancelled is final


async def test_an_instruction_is_received_then_applied(environment: Any) -> None:
    repository, _, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    repository.add_control(dispatch.run_id, "instruction", "Focus on Europe")
    agents, made = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    [control] = repository.controls(dispatch.run_id)
    assert control["status"] == "APPLIED"
    assert control["received_node"] == "plan"
    assert control["received_at"] <= control["applied_at"]
    assert "Focus on Europe" in made["critic"].inputs[0]


async def test_progress_events_are_durable_and_ordered(environment: Any) -> None:
    repository, _, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, _ = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    events = repository.events(dispatch.run_id, after=0)
    completed = [e["payload"]["node"] for e in events if e["kind"] == "step_completed"]
    assert completed == ["plan", "research", "strategy", "critique", "synthesis"]
    cursors = [e["cursor"] for e in events]
    assert cursors == sorted(cursors)
    later = repository.events(dispatch.run_id, after=cursors[2])
    assert [e["cursor"] for e in later] == cursors[3:]
    assert events[-1]["kind"] == "run_finished"


async def test_a_pause_that_arrives_as_the_run_ends_expires_instead(environment: Any) -> None:
    """A finished run is not relabelled paused; the request is marked expired."""
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, made = roles([critique("PASS")])
    chief = made["chief"]
    original = chief.run

    async def pause_while_writing(*args: Any, **kwargs: Any) -> Any:
        repository.add_control(dispatch.run_id, "pause")
        return await original(*args, **kwargs)

    chief.run = pause_while_writing  # type: ignore[method-assign]
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    assert state["status"] == RunStatus.COMPLETED
    [control] = repository.controls(dispatch.run_id)
    assert control["status"] == "EXPIRED"


async def test_a_budget_ceiling_pauses_and_asks_instead_of_failing(environment: Any) -> None:
    """The owner's case: 397k of 400k tokens spent, work done, run marked FAILED.

    Reaching a ceiling is not the work failing. It pauses with the question
    recorded, and everything produced before it stays.
    """
    repository, factory, project_id, url = environment
    dispatch = repository.create(
        project_id, "Assess demand", budget={"max_tokens": 400000, "max_minutes": 30}
    )

    class Exhausted:
        role = "critic"

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.agents.base import AgentOutputError

            raise AgentOutputError("critic", "budget_exhausted:tokens")

    agents, _ = roles([critique("PASS")])
    broken = AgentSet(
        planner=agents.planner,
        researcher=agents.researcher,
        strategist=agents.strategist,
        critic=Exhausted(),  # type: ignore[arg-type]
        chief=agents.chief,
    )
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, broken, saver, 3, "Assess demand", context_for(project_id)
        )

    assert state["status"] == RunStatus.PAUSED
    with factory() as session:
        run = session.get(Run, dispatch.run_id)
        types = sorted(
            row[0]
            for row in session.execute(
                select(Artifact.type).where(Artifact.run_id == dispatch.run_id)
            )
        )
    assert run is not None and run.status == RunStatus.PAUSED
    # The work it had already paid for is still there.
    assert types == ["research_report", "strategy_proposal", "task_plan"]

    asked = repository.pending_budget_ask(dispatch.run_id)
    assert asked is not None
    assert asked["limit"] == "tokens"
    assert asked["node"] == "critic"
    assert asked["budget"]["max_tokens"] == 400000

    # A raise answers the question; nothing is left waiting afterwards.
    repository.add_event(dispatch.run_id, "budget_raised", {"budget": {"max_tokens": 600000}})
    assert repository.pending_budget_ask(dispatch.run_id) is None


async def test_a_run_that_fails_for_any_other_reason_still_fails(environment: Any) -> None:
    """Only a ceiling asks; broken output is still a failure."""
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")

    class Broken:
        role = "critic"

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.agents.base import AgentOutputError

            raise AgentOutputError("critic", "provider_rejected_request")

    agents, _ = roles([critique("PASS")])
    broken = AgentSet(
        planner=agents.planner,
        researcher=agents.researcher,
        strategist=agents.strategist,
        critic=Broken(),  # type: ignore[arg-type]
        chief=agents.chief,
    )
    async with open_checkpointer(url) as saver:
        with pytest.raises(RunFailure):
            await execute_run(
                dispatch, repository, broken, saver, 3, "Assess demand", context_for(project_id)
            )
    assert repository.pending_budget_ask(dispatch.run_id) is None


async def test_reworking_a_completed_run_redoes_the_step_and_keeps_the_old_answer(
    environment: Any,
) -> None:
    """C4: the graph already reached its end; the rework re-enters it as a
    revision and writes the answer again, as a new version."""
    from app.orchestrator.review import ReviewStore

    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, made = roles([critique("PASS")])
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
        assert state["status"] == RunStatus.COMPLETED

        override = ReviewStore(factory).start_cycle(
            dispatch.run_id, "Add the EU vendors", "research", "Alex"
        )
        state = await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            3,
            "Assess demand",
            context_for(project_id),
            resume=True,
            state_override=override,
            resume_node="critique",
        )
    assert state["status"] == RunStatus.COMPLETED
    assert len(made["planner"].calls) == 1, "reworked, not restarted"
    assert len(made["researcher"].calls) == 2
    assert "Add the EU vendors" in made["researcher"].inputs[-1]
    with factory() as session:
        versions = sorted(
            row[0]
            for row in session.execute(
                select(Artifact.version).where(
                    Artifact.run_id == dispatch.run_id, Artifact.type == "synthesis"
                )
            )
        )
    assert versions == [1, 2], "the earlier answer is kept"


async def test_a_task_that_failed_before_its_plan_is_redone_from_the_start(
    environment: Any,
) -> None:
    """C4: with no plan there is no research to redo; the task starts again,
    with the owner's note as direction for every step."""
    from app.orchestrator.review import ReviewStore

    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")

    class Broken:
        role = "planner"

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.agents.base import AgentOutputError

            raise AgentOutputError("planner", "provider_rejected_request")

    agents, made = roles([critique("PASS")])
    broken = AgentSet(
        planner=Broken(),  # type: ignore[arg-type]
        researcher=agents.researcher,
        strategist=agents.strategist,
        critic=agents.critic,
        chief=agents.chief,
    )
    async with open_checkpointer(url) as saver:
        with pytest.raises(RunFailure):
            await execute_run(
                dispatch, repository, broken, saver, 3, "Assess demand", context_for(project_id)
            )
        ReviewStore(factory).restart(dispatch.run_id, "Alex")
        repository.add_control(dispatch.run_id, "instruction", "Focus on the EU")
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    assert state["status"] == RunStatus.COMPLETED
    assert len(made["planner"].calls) == 1
    assert "Focus on the EU" in made["researcher"].inputs[0]


async def test_a_step_planned_task_pauses_between_waves_and_resumes_where_it_was(
    environment: Any,
) -> None:
    """D1: each wave is a checkpointed step, so pause and resume apply between them."""
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Compare vendors")
    agents, made = roles([critique("PASS")])
    made["planner"].outputs = [
        PLAN
        | {
            "task_kind": "research_summary",
            "steps": [
                {"id": "a", "kind": "research", "brief": "do a", "depends_on": []},
                {"id": "w", "kind": "write", "brief": "do w", "depends_on": ["a"]},
            ],
        }
    ]
    repository.add_control(dispatch.run_id, "pause")
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 3, "Compare vendors", context_for(project_id)
        )
        assert state["status"] == RunStatus.PAUSED
        assert repository.requeue(dispatch.run_id)
        state = await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            3,
            "Compare vendors",
            context_for(project_id),
            resume=True,
        )
    assert state["status"] == RunStatus.COMPLETED
    assert len(made["planner"].calls) == 1, "resumed, not replanned"
    assert made["researcher"].calls == ["do a"] and made["strategist"].calls == ["do w"]
    assert state["step_status"] == {"a": "done", "w": "done"}
    with factory() as session:
        types = {
            row[0]
            for row in session.execute(
                select(Artifact.type).where(Artifact.run_id == dispatch.run_id)
            )
        }
    assert {"research_report", "strategy_proposal", "critique", "synthesis"} <= types


async def test_a_long_step_keeps_its_worker_lease(
    environment: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live finding: a round of research ran past the 180s lease, and recovery
    took the task from a worker still running it. The lease is renewed on a
    heartbeat, not only when a step starts."""
    import asyncio

    import app.orchestrator.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "HEARTBEAT_SECONDS", 0.05)
    repository, _, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    renewed: list[str] = []
    real = repository.renew_lease

    def counting(run_id: Any, worker_id: str, *args: Any) -> bool:
        renewed.append(worker_id)
        return real(run_id, worker_id, *args)

    monkeypatch.setattr(repository, "renew_lease", counting)
    agents, made = roles([critique("PASS")])
    slow_run = made["researcher"].run

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.4)
        return await slow_run(*args, **kwargs)

    made["researcher"].run = slow  # type: ignore[method-assign]
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            3,
            "Assess demand",
            context_for(project_id),
            worker_id="w-1",
        )
    # Step starts alone renew five or six times; the heartbeat adds its own.
    assert len(renewed) >= 12, len(renewed)


async def test_a_running_task_shows_where_its_steps_stand_before_it_ends(
    environment: Any,
) -> None:
    """I1: progress is visible while the task runs, not only when it ends."""
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Compare vendors")
    agents, made = roles([critique("PASS")])
    made["planner"].outputs = [
        PLAN
        | {
            "task_kind": "research_summary",
            "steps": [
                {"id": "a", "kind": "research", "brief": "do a", "depends_on": []},
                {"id": "w", "kind": "write", "brief": "do w", "depends_on": ["a"]},
            ],
        }
    ]
    seen: list[Any] = []
    writing = made["strategist"].run

    async def looking(*args: Any, **kwargs: Any) -> Any:
        with factory() as session:
            seen.append(dict(session.get(Run, dispatch.run_id).state_json or {}))  # type: ignore[union-attr]
        return await writing(*args, **kwargs)

    made["strategist"].run = looking  # type: ignore[method-assign]
    async with open_checkpointer(url) as saver:
        await execute_run(
            dispatch, repository, agents, saver, 3, "Compare vendors", context_for(project_id)
        )
    [while_writing] = seen
    assert while_writing["step_status"] == {"a": "done", "w": "pending"}
    assert [s["id"] for s in while_writing["plan"]["steps"]] == ["a", "w"]


async def test_a_room_post_keeps_its_stage_and_what_it_answers(environment: Any) -> None:
    repository, _, project_id, _ = environment
    dispatch = repository.create(project_id, "Assess demand")
    base = {"author_role": "advisor", "step_id": None, "to_role": None, "to_step": None}
    repository.add_team_post(
        dispatch.run_id,
        {**base, "key": "o", "kind": "offer", "text": "Add a step.", "phase": "huddle", "wave": 1},
    )
    repository.add_team_post(
        dispatch.run_id,
        {
            **base,
            "author_role": "chief",
            "key": "a",
            "kind": "plan",
            "text": "Added offer-1.",
            "phase": "huddle",
            "wave": 1,
            "reply_to": "o",
        },
    )
    posts = repository.team_posts(dispatch.run_id)
    assert [(p["phase"], p["wave"], p["reply_to"]) for p in posts] == [
        ("huddle", 1, None),
        ("huddle", 1, "o"),
    ]


# Found on the live office, 6 October: a task started from a discussion (no
# automatic revisions) stopped for review, and every Accept or Revise the owner
# chose sent it straight back to the same stop: 16 decisions, no progress.


async def _stopped_for_review(environment: Any, critiques: list[dict[str, Any]]) -> Any:
    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, made = roles(critiques)
    return dispatch, agents, made


async def test_accepting_a_review_stop_writes_the_answer_with_no_revision_budget(
    environment: Any,
) -> None:
    from app.orchestrator.review import ReviewStore

    repository, factory, project_id, url = environment
    dispatch, agents, made = await _stopped_for_review(
        environment, [critique("REVISE", "research")]
    )
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 0, "Assess demand", context_for(project_id)
        )
        assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
        override = ReviewStore(factory).start_cycle(dispatch.run_id, "", None, "Alex")
        state = await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            0,
            "Assess demand",
            context_for(project_id),
            resume=True,
            state_override=override,
            resume_node="critique",
        )
    assert state["status"] == RunStatus.COMPLETED, state.get("stopped_reason")
    assert len(made["chief"].calls) >= 1, "the answer is written"


async def test_the_owners_revise_runs_once_more_with_no_revision_budget(
    environment: Any,
) -> None:
    from app.orchestrator.review import ReviewStore

    repository, factory, project_id, url = environment
    dispatch, agents, made = await _stopped_for_review(
        environment, [critique("REVISE", "research"), critique("PASS")]
    )
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 0, "Assess demand", context_for(project_id)
        )
        assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
        researched = len(made["researcher"].calls)
        override = ReviewStore(factory).start_cycle(
            dispatch.run_id, "Add the EU vendors", "research", "Alex"
        )
        state = await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            0,
            "Assess demand",
            context_for(project_id),
            resume=True,
            state_override=override,
            resume_node="critique",
        )
    assert len(made["researcher"].calls) == researched + 1, "the owner's revision ran"
    assert state["status"] == RunStatus.COMPLETED, state.get("stopped_reason")


async def test_research_stopped_by_a_provider_outage_runs_again_on_one_press(
    environment: Any,
) -> None:
    """6 Oct: providers down stops the task saying so; the owner's retry runs."""
    from dataclasses import replace

    from app.agents.base import AgentOutputError
    from app.orchestrator.review import ReviewStore
    from app.schemas import ResearchReport
    from tests.unit.test_graph import PLAN

    repository, factory, project_id, url = environment
    dispatch = repository.create(project_id, "Assess demand")
    agents, made = roles([critique("PASS")])
    made["planner"].outputs = [
        PLAN
        | {
            "task_kind": "research_summary",
            "steps": [
                {"id": "a", "kind": "research", "brief": "do a", "depends_on": []},
                {"id": "w", "kind": "write", "brief": "do w", "depends_on": ["a"]},
            ],
        }
    ]

    class Recovering:
        role = "researcher"

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.down = True

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            self.calls.append(objective)
            if self.down:
                raise AgentOutputError("researcher", "rate_limited")
            return ResearchReport.model_validate(RESEARCH)

    researcher = Recovering()
    agents = replace(agents, researcher=researcher)  # type: ignore[arg-type]
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, agents, saver, 0, "Assess demand", context_for(project_id)
        )
        assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
        assert state["stopped_reason"] == "providers_unavailable"
        researcher.down = False
        override = ReviewStore(factory).start_cycle(
            dispatch.run_id, "Try the research again.", "research", "Alex"
        )
        state = await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            0,
            "Assess demand",
            context_for(project_id),
            resume=True,
            state_override=override,
            resume_node="critique",
        )
    assert researcher.calls.count("do a") >= 2, "the research ran again"
    assert state["status"] == RunStatus.COMPLETED, state.get("stopped_reason")


class SpentCritic:
    role = "critic"

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        from app.agents.base import AgentOutputError

        raise AgentOutputError("critic", "budget_exhausted:tokens")


async def paused_at_ceiling(environment: Any, chief: Any = None) -> tuple[Any, Any, Any]:
    repository, factory, project_id, url = environment
    dispatch = repository.create(
        project_id, "Assess demand", budget={"max_tokens": 400000, "max_minutes": 30}
    )
    agents, made = roles([critique("PASS")])
    spent = AgentSet(
        planner=agents.planner,
        researcher=agents.researcher,
        strategist=agents.strategist,
        critic=SpentCritic(),  # type: ignore[arg-type]
        chief=chief or agents.chief,
    )
    async with open_checkpointer(url) as saver:
        state = await execute_run(
            dispatch, repository, spent, saver, 3, "Assess demand", context_for(project_id)
        )
    assert state["status"] == RunStatus.PAUSED
    return dispatch, spent, made


async def finish_now(environment: Any, dispatch: Any, agents: Any) -> Any:
    from app.orchestrator.review import ReviewStore

    repository, factory, project_id, url = environment
    override = ReviewStore(factory).finish(dispatch.run_id, "Alex")
    async with open_checkpointer(url) as saver:
        return await execute_run(
            dispatch,
            repository,
            agents,
            saver,
            3,
            "Assess demand",
            context_for(project_id),
            resume=True,
            state_override=override,
            resume_node="critique",
        )


async def test_finishing_a_task_at_its_ceiling_writes_the_answer_from_its_work(
    environment: Any,
) -> None:
    """6 Oct: stopping a task at its limit kept nothing of what it found."""
    dispatch, agents, made = await paused_at_ceiling(environment)
    researched = len(made["researcher"].calls)
    state = await finish_now(environment, dispatch, agents)
    assert state["status"] == RunStatus.COMPLETED
    assert state["synthesis"]["recommendation"]
    assert len(made["researcher"].calls) == researched, "nothing more was researched"
    assert "finished this task early" in made["chief"].inputs[-1]


async def test_finishing_when_the_last_writing_pass_cannot_run_keeps_the_notes(
    environment: Any,
) -> None:
    """A refused last call must not turn the work into nothing."""

    class SpentChief:
        role = "chief"

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.agents.base import AgentOutputError

            raise AgentOutputError("chief", "budget_exhausted:tokens")

    dispatch, agents, _ = await paused_at_ceiling(environment, chief=SpentChief())
    state = await finish_now(environment, dispatch, agents)
    assert state["status"] == RunStatus.COMPLETED
    assert state["written_from_notes"] == "budget_exhausted:tokens"
    assert state["synthesis"]["facts"] == RESEARCH["key_findings"]


async def test_finishing_gives_the_answer_without_asking_to_act_on_it(
    environment: Any,
) -> None:
    """6 Oct: a finished task stopped again to ask approval for what it proposed."""
    from tests.unit.test_graph import SYNTHESIS

    dispatch, agents, made = await paused_at_ceiling(environment)
    made["chief"].outputs = [SYNTHESIS | {"proposes_action": True}]
    state = await finish_now(environment, dispatch, agents)
    assert state["status"] == RunStatus.COMPLETED
    assert state["synthesis"]["proposes_action"] is False
    assert state["synthesis"]["proposed_engineering_scope"] == ""
    assert state["synthesis"]["recommendation"] == "r", "the recommendation is kept"
