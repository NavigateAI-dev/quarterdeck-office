"""V2: the advisor on call — consulted at three moments, never writing the work."""

from dataclasses import replace
from typing import Any

from app.agents.base import AgentOutputError
from app.orchestrator.graph import ADVISOR_CHARS, run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import RunStatus
from app.schemas.agent_output import Advice
from tests.unit.test_graph import RESEARCH, Role, agents, critique, start
from tests.unit.test_step_executor import EMPTY, planned, step

CONTINUE = {"verdict": "continue", "advice": [], "watch_for": ["dates"], "confidence": 0.8}
CHANGE = {
    "verdict": "change_course",
    "advice": ["Read the regulator's register, not news reports"],
    "watch_for": [],
    "confidence": 0.9,
}


class Down:
    role = "advisor"

    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        self.calls += 1
        raise AgentOutputError("advisor", self.code)


async def run(
    plans: list[dict[str, Any]],
    critiques: list[dict[str, Any]],
    advisor: Any = None,
    research: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    agent_set, roles = agents(critiques)
    roles["planner"].outputs = plans
    if research is not None:
        roles["researcher"].outputs = research
    if advisor is not None:
        agent_set = replace(agent_set, advisor=advisor)
        roles["advisor"] = advisor
    state = await run_analysis(build_graph(agent_set, 3), start())
    return state, roles


def moments(state: Any) -> list[str]:
    return [entry["moment"] for entry in state.get("advice") or []]


async def test_without_an_advisor_nothing_changes() -> None:
    state, roles = await run([planned(step("a"))], [critique("PASS")])
    assert state["status"] == RunStatus.COMPLETED
    assert "advice" not in state or not state["advice"]
    assert len(roles["planner"].calls) == 1


async def test_the_plan_is_reviewed_and_kept_when_the_advisor_agrees() -> None:
    advisor = Role("advisor", Advice, [CONTINUE])
    state, roles = await run([planned(step("a"))], [critique("PASS")], advisor)
    assert moments(state) == ["before_plan", "before_done"]
    assert len(roles["planner"].calls) == 1, "continue does not cost a second plan"
    assert "do a" in advisor.inputs[0], "the advisor reads the plan"


async def test_a_change_of_course_has_the_chief_revise_the_plan_once() -> None:
    advisor = Role("advisor", Advice, [CHANGE, CONTINUE])
    state, roles = await run(
        [planned(step("a")), planned(step("register"))], [critique("PASS")], advisor
    )
    assert len(roles["planner"].calls) == 2
    assert "Read the regulator's register" in roles["planner"].inputs[1]
    assert list(state["step_status"]) == ["register"], "the revised plan is the one run"
    assert state["advice"][0]["verdict"] == "change_course"


async def test_a_first_replan_is_routine_and_a_second_is_a_repeat() -> None:
    advisor = Role("advisor", Advice, [CONTINUE])
    # A second replan follows a wave that got somewhere: after two waves that
    # found nothing, the work is written with its gaps instead.
    state, roles = await run(
        [planned(step("a")), planned(step("b"), step("b2")), planned(step("c"))],
        [critique("PASS")],
        advisor,
        research=[EMPTY, RESEARCH, EMPTY],
    )
    assert state["replans"] == 2
    assert moments(state) == ["before_plan", "repeated_failure", "before_done"]
    # The second replan's planner call carries the advice.
    assert "advisor" in roles["planner"].inputs[-1].lower()


async def test_the_answer_is_written_with_the_advice_in_hand() -> None:
    advisor = Role("advisor", Advice, [CONTINUE, CHANGE])
    _, roles = await run([planned(step("a"))], [critique("PASS")], advisor)
    assert "Read the regulator's register" in roles["chief"].inputs[-1]


async def test_an_advisor_that_fails_is_skipped_and_says_so() -> None:
    advisor = Down("invalid_output")
    state, _ = await run([planned(step("a"))], [critique("PASS")], advisor)
    assert state["status"] == RunStatus.COMPLETED
    assert [entry["verdict"] for entry in state["advice"]] == ["unavailable", "unavailable"]


async def test_an_advisor_never_pauses_a_task_for_its_own_cost() -> None:
    advisor = Down("budget_exhausted:tokens")
    state, _ = await run([planned(step("a"))], [critique("PASS")], advisor)
    assert state["status"] == RunStatus.COMPLETED
    assert state["advice"][0]["verdict"] == "skipped"


async def test_the_advisor_reads_a_bounded_view() -> None:
    advisor = Role("advisor", Advice, [CONTINUE])
    huge = RESEARCH | {"key_findings": ["x" * (ADVISOR_CHARS * 2)]}
    await run([planned(step("a"))], [critique("PASS")], advisor, research=[huge])
    assert all(len(given) <= ADVISOR_CHARS + 2000 for given in advisor.inputs)


def test_a_run_gets_an_advisor_only_when_it_is_on() -> None:
    from app.agents import AdvisorAgent
    from app.config import Settings
    from app.runtime.worker import build_agents

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost:5432/x",
    )
    assert build_agents(object(), settings).advisor is None  # type: ignore[arg-type]
    on = build_agents(object(), settings, advisor=True)  # type: ignore[arg-type]
    assert isinstance(on.advisor, AdvisorAgent)
    assert on.advisor.model_alias == settings.chief_model_alias


class Memory:
    """Where the dispatcher keeps each consultation as it is made."""

    def __init__(self) -> None:
        self.kept: list[dict[str, Any]] = []

    def recall(self) -> list[dict[str, Any]]:
        return list(self.kept)

    def keep(self, entries: list[dict[str, Any]]) -> None:
        self.kept = list(entries)


class PlannerThenBudget:
    """Plans once, then is refused by the budget: the task pauses."""

    role = "planner"

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.calls = 0

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        from app.schemas import TaskPlan
        from tests.unit.test_graph import vary

        self.calls += 1
        if self.calls > 1:
            raise AgentOutputError("planner", "budget_exhausted:tokens")
        return TaskPlan.model_validate(vary(self.plan, 1))


async def test_advice_given_before_a_pause_is_not_bought_twice() -> None:
    """The owner's task consulted the advisor, paused at its limit, and on
    resuming paid for the same consultation again."""
    import pytest

    from app.orchestrator.graph import RunFailure

    memory = Memory()
    plan = planned(step("a"))
    first, _ = agents([critique("PASS")])
    first = replace(
        first,
        planner=PlannerThenBudget(plan),
        advisor=Role("advisor", Advice, [CHANGE]),
    )
    with pytest.raises(RunFailure):
        await run_analysis(build_graph(first, 3, advice_memory=memory), start())
    assert [entry["moment"] for entry in memory.kept] == ["before_plan"]

    # Resumed: the same moment in the same state reuses what was said.
    again, roles = agents([critique("PASS")])
    roles["planner"].outputs = [plan, planned(step("register"))]
    advisor = Role("advisor", Advice, [CONTINUE])
    again = replace(again, advisor=advisor)
    state = await run_analysis(build_graph(again, 3, advice_memory=memory), start())
    assert state["status"] == RunStatus.COMPLETED
    assert len(advisor.calls) == 1, "only before_done is asked; before_plan is remembered"
    assert "Read the regulator's register" in roles["planner"].inputs[1]
