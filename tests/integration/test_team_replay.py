"""A team run plays again from its kept calls, with no model.

The office's own roles, built as the worker builds them, answer a task once
through a scripted model whose every call is kept. The same task then runs
again with each role answered from those calls alone.
"""

import json
from typing import Any

import pytest

from app.agent_routing import keep, members
from app.config import Settings
from app.gateway.client import Completion, Usage
from app.model_calls import ModelCalls, ReplayCompleter
from app.orchestrator.checkpoints import open_checkpointer
from app.orchestrator.dispatcher import execute_run
from app.orchestrator.state import RunStatus
from app.runtime.worker import build_agents
from app.schemas import Critique, ResearchReport, StrategyProposal, Synthesis, TaskPlan
from app.tools.web import SearchResult, StaticSearchTool
from tests.integration.test_dispatch import context_for, environment  # noqa: F401 - fixture
from tests.unit.test_graph import PLAN, RESEARCH, STRATEGY, SYNTHESIS, critique

pytestmark = pytest.mark.integration

ANSWERS: dict[Any, dict[str, Any]] = {
    TaskPlan: PLAN,
    ResearchReport: RESEARCH,
    StrategyProposal: STRATEGY,
    Critique: critique("PASS"),
    Synthesis: SYNTHESIS,
}


class Scripted:
    """Answers each role by the schema it asks for; names any call it was not told about."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def complete(
        self, model_alias: str, messages: list[dict[str, Any]], **options: Any
    ) -> Completion:
        schema = options.get("schema")
        self.asked.append(model_alias)
        if schema not in ANSWERS:
            raise AssertionError(f"{model_alias} asked for {schema!r}: not scripted")
        return Completion(
            model_alias=model_alias,
            model="scripted",
            content=json.dumps(ANSWERS[schema]),
            usage=Usage(),
            latency_seconds=0.0,
        )


def office(model: Any) -> Any:
    """The roles as the worker builds them, with one fixed search result."""
    agents = build_agents(
        model, Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/x")
    )
    agents.researcher.search = StaticSearchTool(  # type: ignore[attr-defined]
        [SearchResult("t", "https://example.com", "snippet", source_type="primary")]
    )
    return agents


async def test_a_team_run_plays_again_from_its_kept_calls_with_no_model(
    environment: Any,  # noqa: F811
) -> None:
    repository, factory, project_id, url = environment
    calls = ModelCalls(factory)
    model = Scripted()
    first = repository.create(project_id, "Assess demand")
    agents = keep(office(model), calls, first.run_id)
    async with open_checkpointer(url) as saver:
        asked = await execute_run(
            first, repository, agents, saver, 3, "Assess demand", context_for(project_id)
        )
    assert asked["status"] == RunStatus.COMPLETED
    kept = calls.of_run(first.run_id)
    assert len(kept) == len(model.asked)
    assert {c["role"] for c in kept} >= {"planner", "researcher", "strategist", "critic", "chief"}

    # The same task again, answered from what was kept: no model, no key.
    replay = ReplayCompleter(calls.recorded(first.run_id))
    again = repository.create(project_id, "Assess demand")
    replayed = office(Scripted())
    for agent in members(replayed):
        agent.gateway = replay
    async with open_checkpointer(url) as saver:
        played = await execute_run(
            again, repository, replayed, saver, 3, "Assess demand", context_for(project_id)
        )

    assert played["status"] == RunStatus.COMPLETED
    assert played["synthesis"] == asked["synthesis"]
    assert played["research"] == asked["research"]
    # Every kept answer was asked for again, and nothing else was.
    assert all(not waiting for waiting in replay.answers.values())
