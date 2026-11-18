import os
from typing import Any
from uuid import uuid4

import pytest

from app.orchestrator.checkpoints import checkpoint_dsn, open_checkpointer, thread_config
from app.orchestrator.graph import AgentSet, RunFailure, run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import RunStatus
from app.schemas import Critique, ResearchReport, StrategyProposal, Synthesis, TaskPlan
from tests.unit.test_graph import PLAN, RESEARCH, STRATEGY, SYNTHESIS, Role, critique, start


class Breaking:
    """Fails once, then defers to a working role."""

    role = "critic"

    def __init__(self, inner: Role) -> None:
        self.inner = inner
        self.broken = True

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        if self.broken:
            raise RuntimeError("process died mid-critique")
        return await self.inner.run(objective, context, inputs)


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    return url


def test_dsn_drops_the_sqlalchemy_driver_suffix() -> None:
    dsn = checkpoint_dsn("postgresql+psycopg://user:secret@localhost:5432/quarterdeck")
    assert dsn.startswith("postgresql://")
    assert "+psycopg" not in dsn


@pytest.mark.integration
async def test_setup_is_idempotent() -> None:
    url = database_url()
    async with open_checkpointer(url):
        pass
    async with open_checkpointer(url) as saver:
        assert saver is not None


@pytest.mark.integration
async def test_interrupted_run_resumes_without_repeating_completed_nodes() -> None:
    url = database_url()
    roles = {
        "planner": Role("planner", TaskPlan, [PLAN]),
        "researcher": Role("researcher", ResearchReport, [RESEARCH]),
        "strategist": Role("strategist", StrategyProposal, [STRATEGY]),
        "critic": Role("critic", Critique, [critique("PASS")]),
        "chief": Role("chief", Synthesis, [SYNTHESIS]),
    }
    breaking = Breaking(roles["critic"])
    run_id = str(uuid4())
    config = thread_config(run_id)

    async with open_checkpointer(url) as saver:
        agents = AgentSet(
            planner=roles["planner"],  # type: ignore[arg-type]
            researcher=roles["researcher"],  # type: ignore[arg-type]
            strategist=roles["strategist"],  # type: ignore[arg-type]
            critic=breaking,  # type: ignore[arg-type]
            chief=roles["chief"],  # type: ignore[arg-type]
        )
        graph = build_graph(agents, 3, checkpointer=saver)
        with pytest.raises((RunFailure, RuntimeError)):
            await run_analysis(graph, start(), config)

    assert len(roles["planner"].calls) == 1
    assert len(roles["researcher"].calls) == 1
    assert len(roles["strategist"].calls) == 1

    # A fresh process: new checkpointer, new graph, same thread ID.
    breaking.broken = False
    async with open_checkpointer(url) as saver:
        agents = AgentSet(
            planner=roles["planner"],  # type: ignore[arg-type]
            researcher=roles["researcher"],  # type: ignore[arg-type]
            strategist=roles["strategist"],  # type: ignore[arg-type]
            critic=breaking,  # type: ignore[arg-type]
            chief=roles["chief"],  # type: ignore[arg-type]
        )
        graph = build_graph(agents, 3, checkpointer=saver)
        state = await run_analysis(graph, None, config)  # type: ignore[arg-type]

    assert state["status"] == RunStatus.COMPLETED
    # Completed nodes were restored from the checkpoint, not re-executed.
    assert len(roles["planner"].calls) == 1
    assert len(roles["researcher"].calls) == 1
    assert len(roles["strategist"].calls) == 1
    assert len(roles["chief"].calls) == 1
