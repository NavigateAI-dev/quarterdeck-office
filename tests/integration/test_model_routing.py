"""The model router at task start, on a real database.

Shaped like the office on 16 Oct 2026: a strong Chief and cheap other roles,
each on its chosen connection (on the gateway until 22 Oct), and three cheap
tested connections.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.db.models import ModelConnection, OfficeSettings, Project
from app.orchestrator.graph import AgentSet
from app.runtime import model_stack
from app.runtime.job import Job
from app.runtime.model_stack import ModelStack
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture
from tests.unit.test_direct_completion import settings

pytestmark = pytest.mark.integration

CATALOGUE = {
    "claude-haiku-5-5": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
    "gpt-5.6-luna": {"input_cost_per_token": 2e-7, "output_cost_per_token": 1.2e-6},
    "deepseek-flash": {"input_cost_per_token": 3e-7, "output_cost_per_token": 1.2e-6},
    "gpt-5.6-sol": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5},
    "gpt-5.4-nano": {"input_cost_per_token": 2.5e-7, "output_cost_per_token": 1.2e-6},
}
# Each role's chosen model, as the gateway's roles were adopted on 22 Oct.
ROLE_MODELS = {
    "chief": ("openai", "gpt-5.6-sol"),
    "researcher": ("openai", "gpt-5.4-nano"),
    "strategist": ("deepseek", "deepseek-flash"),
    "critic": ("openai", "gpt-5.6-luna"),
}
ALIASES = {"chief": "0001", "researcher": "0002", "strategist": "0003", "critic": "0004"}


class Judge:
    def __init__(self, answer: tuple[str, float]) -> None:
        self.answer = answer
        self.asked = 0

    async def model_route(self, task: str, instructions: str, criteria: dict[str, str]) -> Any:
        self.asked += 1
        return self.answer


REQUIRED = ("planner", "researcher", "strategist", "critic", "chief")


def team() -> AgentSet:
    """Stand-in agents by role alias; the planner shares the Chief's model."""
    named = {
        role: SimpleNamespace(role=role, model_alias=ALIASES.get(role, "0001")) for role in REQUIRED
    }
    return AgentSet(**named)  # type: ignore[arg-type]


@pytest.fixture
def office(environment: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    repository, factory, project_id, _ = environment
    ids = {}
    with factory() as session, session.begin():
        for provider, model in (
            ("anthropic", "claude-haiku-5-5"),
            ("openai", "gpt-5.6-luna"),
            ("deepseek", "deepseek-flash"),
        ):
            row = ModelConnection(
                provider=provider,
                display_name=model,
                model_id=model,
                credential_ref="keychain:test",
                status="MODEL_AVAILABLE",
            )
            session.add(row)
            session.flush()
            ids[model] = str(row.id)

    async def catalogue(root: Any) -> dict[str, Any]:
        return CATALOGUE

    judge = Judge(("fast", 0.9))
    monkeypatch.setattr(model_stack, "price_map", catalogue)
    monkeypatch.setattr(model_stack, "build_judge", lambda config: judge)
    stack = ModelStack(settings(), repository, factory)
    dispatch = repository.create(project_id, "What is the Dutch VAT rate?")
    job = Job(dispatch=dispatch, objective="What is the Dutch VAT rate?", context={})
    roles = {
        role: {"connection_id": f"role-{role}", "provider": provider, "model_id": model}
        for role, (provider, model) in ROLE_MODELS.items()
    }
    return SimpleNamespace(
        stack=stack,
        job=job,
        judge=judge,
        ids=ids,
        factory=factory,
        repository=repository,
        roles=roles,
    )


def routed_event(office: Any) -> dict[str, Any]:
    [event] = [
        e
        for e in office.repository.events(office.job.dispatch.run_id)
        if e["kind"] == "model_routed"
    ]
    return dict(event["payload"])


async def test_an_easy_task_moves_every_dear_role_to_the_fast_model(office: Any) -> None:
    entries = await office.stack.route(office.job, team(), office.roles, set())

    assert {role: e["model_id"] for role, e in entries.items()} == dict.fromkeys(
        REQUIRED, "claude-haiku-5-5"
    )
    routed = routed_event(office)
    assert (routed["tier"], routed["model_id"]) == ("fast", "claude-haiku-5-5")
    assert routed["pool"] == {"fast": "claude-haiku-5-5", "balanced": "gpt-5.6-luna"}


async def test_an_ordinary_task_moves_only_the_strong_roles(office: Any) -> None:
    office.judge.answer = ("balanced", 0.85)

    entries = await office.stack.route(office.job, team(), office.roles, set())

    moved = {role for role, e in entries.items() if e.get("model_id") == "gpt-5.6-luna"}
    assert moved == {"chief", "planner", "critic"}, "the critic was on it already"
    assert entries["researcher"] == office.roles["researcher"]


async def test_a_hard_task_keeps_the_office_setup(office: Any) -> None:
    office.judge.answer = ("performance", 0.9)

    assert await office.stack.route(office.job, team(), office.roles, set()) == office.roles
    assert routed_event(office)["tier"] == "performance"


async def test_a_resumed_task_keeps_its_moves_without_asking_again(office: Any) -> None:
    first = await office.stack.route(office.job, team(), office.roles, set())

    again = await office.stack.route(office.job, team(), office.roles, set())

    assert office.judge.asked == 1
    assert again == first


async def test_roles_an_agent_or_the_project_pinned_keep_their_model(office: Any) -> None:
    with office.factory() as session, session.begin():
        project = session.get(Project, office.job.dispatch.project_id)
        project.role_models_json = {"critic": office.ids["deepseek-flash"]}
    pinned = {
        "connection_id": office.ids["deepseek-flash"],
        "provider": "deepseek",
        "model_id": "deepseek-flash",
    }

    entries = await office.stack.route(
        office.job, team(), office.roles | {"critic": pinned}, {"chief"}
    )

    assert entries["critic"] == pinned
    assert entries["chief"] == office.roles["chief"], "an agent's own model stays"
    assert entries["researcher"]["model_id"] == "claude-haiku-5-5"


async def test_fixed_routing_leaves_the_office_setup_alone(office: Any) -> None:
    with office.factory() as session, session.begin():
        session.add(OfficeSettings(only=True, model_routing="fixed"))

    assert await office.stack.route(office.job, team(), office.roles, set()) == office.roles
    assert office.judge.asked == 0


async def test_without_a_judge_nothing_is_fetched_and_the_setup_stands(
    office: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*args: Any) -> Any:
        raise AssertionError("fetched although the router cannot run")

    monkeypatch.setattr(model_stack, "build_judge", lambda config: None)
    monkeypatch.setattr(model_stack, "price_map", never)

    assert await office.stack.route(office.job, team(), office.roles, set()) == office.roles
    assert routed_event(office)["outcome"] == "unavailable"
