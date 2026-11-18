from typing import Any
from uuid import uuid4

import pytest

from app.agents.base import AgentOutputError
from app.orchestrator.graph import AgentSet, RunFailure, run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import Node, RunStatus, initial_state
from app.schemas import Critique, ResearchReport, StrategyProposal, Synthesis, TaskPlan

PLAN = {
    "objective": "o",
    "task_kind": "product_proposal",
    "research_question": "Do agencies need this?",
    "acceptance_criteria": ["ten sources"],
    "expected_artifacts": ["research/report.md"],
}

SUMMARY_PLAN = PLAN | {"task_kind": "research_summary"}

UNREACHABLE_PLAN = PLAN | {
    "evidence_reachable": False,
    "unreachable_evidence": ["Internal onboarding records"],
}

SUMMARY = {
    "question": "How do they compare?",
    "findings": ["f"],
    "limits": ["Only secondary sources were available"],
    "recommendation": "r",
}
RESEARCH = {
    "research_question": "q",
    "key_findings": ["f"],
    "evidence": ["e"],
    "implications": ["i"],
    "sources": [{"title": "t", "url": "https://example.com", "source_type": "primary"}],
    "confidence": "medium",
}
STRATEGY = {
    field: "x"
    for field in (
        "problem",
        "who_has_it",
        "frequency_severity",
        "current_behavior",
        "willingness_to_change",
        "smallest_useful_test",
        "invalidation_condition",
        "icp",
        "jtbd",
    )
} | {"supporting_evidence": ["e"], "mvp_scope": ["m"], "success_metrics": ["s"]}
SYNTHESIS = {"facts": ["f"], "recommendation": "r", "proposed_engineering_scope": "s"}


def critique(verdict: str, target: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"verdict": verdict, "confidence": "medium"}
    if target:
        body["revision_target"] = target
    return body


def vary(payload: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Make a rerun's output differ, as a real revision would.

    Identical output across a revision is treated as no progress, so a fixture
    that repeated itself would stop the loop instead of exercising it.
    """
    if attempt <= 1:
        return payload
    for key, value in payload.items():
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return {**payload, key: [*value, f"revision {attempt}"]}
    return payload


class Role:
    """Records each invocation and returns queued outputs in order."""

    def __init__(self, role: str, schema: type, outputs: list[dict[str, Any]]) -> None:
        self.role = role
        self.schema = schema
        self.outputs = list(outputs)
        self.calls: list[str] = []
        self.inputs: list[str] = []

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        self.calls.append(objective)
        self.inputs.append(inputs)
        payload = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return self.schema.model_validate(vary(payload, len(self.calls)))


class Failing:
    role = "researcher"

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        raise AgentOutputError("researcher", "no_search_results")


def agents(critiques: list[dict[str, Any]]) -> tuple[AgentSet, dict[str, Role]]:
    roles = {
        "planner": Role("planner", TaskPlan, [PLAN]),
        "researcher": Role("researcher", ResearchReport, [RESEARCH]),
        "strategist": Role("strategist", StrategyProposal, [STRATEGY]),
        "critic": Role("critic", Critique, critiques),
        "chief": Role("chief", Synthesis, [SYNTHESIS]),
    }
    return AgentSet(**roles), roles  # type: ignore[arg-type]


def start() -> Any:
    return initial_state(
        "Assess demand for feature X",
        {
            "organization": "Northwind",
            "project_id": str(uuid4()),
            "project_name": "Harbor AI",
        },
    )


async def execute(critiques: list[dict[str, Any]], max_revisions: int = 3) -> tuple[Any, dict]:
    agent_set, roles = agents(critiques)
    graph = build_graph(agent_set, max_revisions)
    return await run_analysis(graph, start()), roles


async def test_pass_chain_persists_every_stage() -> None:
    state, roles = await execute([critique("PASS")])
    assert state["status"] == RunStatus.COMPLETED
    assert Synthesis.model_validate(state["synthesis"]).recommendation == "r"
    assert all(len(role.calls) == 1 for role in roles.values())


async def test_research_question_comes_from_the_plan() -> None:
    _, roles = await execute([critique("PASS")])
    assert roles["researcher"].calls == ["Do agencies need this?"]


async def test_each_stage_receives_its_upstream_artifacts() -> None:
    _, roles = await execute([critique("PASS")])
    assert "Research" in roles["strategist"].inputs[0]
    assert "Strategy" in roles["critic"].inputs[0]
    assert "Critique" in roles["chief"].inputs[0]


async def test_research_revision_reruns_strategy_and_critique() -> None:
    state, roles = await execute([critique("REVISE", "research"), critique("PASS")])
    assert [
        len(roles[name].calls) for name in ("planner", "researcher", "strategist", "critic")
    ] == [
        1,
        2,
        2,
        2,
    ]
    assert state["revision_count"] == 1
    assert state["status"] == RunStatus.COMPLETED


async def test_strategy_revision_does_not_rerun_research() -> None:
    state, roles = await execute([critique("REVISE", "strategy"), critique("PASS")])
    assert len(roles["researcher"].calls) == 1
    assert len(roles["strategist"].calls) == 2
    assert state["revision_count"] == 1


async def test_block_ends_without_synthesis() -> None:
    state, roles = await execute([critique("BLOCK")])
    assert state["status"] == RunStatus.BLOCKED
    assert state["stopped_reason"] == "critic_blocked"
    assert state["synthesis"] is None
    assert roles["chief"].calls == []


def differing_critique(target: str, issue: str) -> dict[str, Any]:
    """A critique that differs each cycle, so the loop is genuinely revising."""
    return critique("REVISE", target) | {"critical_issues": [issue]}


async def test_revision_exhaustion_stops_for_human_review() -> None:
    state, roles = await execute(
        [
            differing_critique("research", "first gap"),
            differing_critique("research", "second gap"),
            differing_critique("research", "third gap"),
        ],
        max_revisions=2,
    )
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "revision_limit_reached"
    assert state["synthesis"] is None
    assert state["revision_count"] == 2
    assert len(roles["researcher"].calls) == 3


async def test_a_restated_critique_stops_the_loop_early() -> None:
    """Real critiques are never byte-identical; overlap is what matters."""
    first = critique("REVISE", "strategy") | {
        "missing_evidence": ["Direct interviews with agency owners about research hours"]
    }
    restated = critique("REVISE", "strategy") | {
        "missing_evidence": ["Interviews with owners of agencies covering research hours"]
    }
    state, roles = await execute([first, restated, critique("PASS")], max_revisions=5)
    assert state["stopped_reason"] == "revision_not_converging"
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert len(roles["critic"].calls) == 2


async def test_a_pass_that_still_names_its_limits_completes_the_task() -> None:
    """A passing critique lists what the evidence leaves open; that is not a loop.

    Seen on a live task: the critic sent the research back once, then passed the
    revision while still recording the gaps it could not close. The repeat check
    compared those gaps with the first round's concerns, found them all again,
    and stopped the task as not converging, throwing the pass away.
    """
    gaps = ["A complete current register extract of Dutch-authorized EMIs"]
    first = critique("REVISE", "research") | {"missing_evidence": gaps}
    passed = critique("PASS") | {"missing_evidence": gaps}
    state, roles = await execute([first, passed], max_revisions=5)
    assert state["status"] == RunStatus.COMPLETED
    assert state.get("stopped_reason") in (None, "")
    assert len(roles["chief"].calls) == 1


async def test_a_genuinely_different_critique_continues_the_loop() -> None:
    """A revision that raises new concerns is progress, not a loop."""
    first = critique("REVISE", "strategy") | {
        "missing_evidence": ["Pricing tiers and published rate limits"]
    }
    different = critique("REVISE", "strategy") | {
        "missing_evidence": ["Competitor feature comparison across vendors"]
    }
    state, roles = await execute([first, different, critique("PASS")], max_revisions=5)
    assert state["status"] == RunStatus.COMPLETED
    assert len(roles["critic"].calls) == 3


async def test_identical_critiques_still_stop_the_loop() -> None:
    state, roles = await execute([critique("REVISE", "strategy")], max_revisions=5)
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "revision_not_converging"
    assert state["synthesis"] is None
    # Stopped on the second identical critique, well inside the budget.
    assert len(roles["critic"].calls) == 2
    assert state["revision_count"] < 5


async def test_a_revision_that_changes_nothing_stops_the_loop() -> None:
    """A rerun producing the same artifact cannot improve anything downstream."""
    agent_set, roles = agents([differing_critique("strategy", "one"), critique("PASS")])
    # A strategist that ignores the critique and repeats itself.
    roles["strategist"].run = _repeating(roles["strategist"])  # type: ignore[method-assign]
    graph = build_graph(agent_set, 5)
    state = await run_analysis(graph, start())
    assert state["stopped_reason"] == "revision_made_no_change"
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert len(roles["critic"].calls) == 1


def _repeating(role: Any) -> Any:
    """Return a run() that always yields the role's first output verbatim."""

    async def run(objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        role.calls.append(objective)
        role.inputs.append(inputs)
        return role.schema.model_validate(role.outputs[0])

    return run


async def test_stale_artifacts_are_cleared_on_research_revision() -> None:
    agent_set, roles = agents([critique("REVISE", "research"), critique("PASS")])
    graph = build_graph(agent_set, 3)
    await run_analysis(graph, start())
    rerun_inputs = roles["strategist"].inputs[1]
    assert "Critique" not in rerun_inputs


async def test_agent_failure_stops_the_run_with_its_node() -> None:
    agent_set, _ = agents([critique("PASS")])
    broken = AgentSet(
        planner=agent_set.planner,
        researcher=Failing(),  # type: ignore[arg-type]
        strategist=agent_set.strategist,
        critic=agent_set.critic,
        chief=agent_set.chief,
    )
    with pytest.raises(RunFailure) as caught:
        await run_analysis(build_graph(broken, 3), start())
    assert (caught.value.node, caught.value.code) == ("researcher", "no_search_results")


async def test_node_names_are_stable() -> None:
    graph = build_graph(agents([critique("PASS")])[0], 3)
    assert {Node.PLAN, Node.RESEARCH, Node.STRATEGY, Node.CRITIQUE, Node.SYNTHESIS} <= set(
        graph.get_graph().nodes
    )


async def test_a_research_summary_uses_its_own_contract() -> None:
    """A request to compare is not a product hypothesis."""
    from app.schemas import ResearchSummary

    agent_set, roles = agents([critique("PASS")])
    summarist = Role("summarist", ResearchSummary, [SUMMARY])
    roles["planner"].outputs = [SUMMARY_PLAN]
    graph = build_graph(
        AgentSet(
            planner=roles["planner"],  # type: ignore[arg-type]
            researcher=agent_set.researcher,
            strategist=agent_set.strategist,
            critic=agent_set.critic,
            chief=agent_set.chief,
            summarist=summarist,  # type: ignore[arg-type]
        ),
        3,
    )
    state = await run_analysis(graph, start())
    assert state["status"] == RunStatus.COMPLETED
    # The summarist produced the deliverable; the strategist was not invoked.
    assert len(summarist.calls) == 1
    assert roles["strategist"].calls == []
    assert ResearchSummary.model_validate(state["strategy"]).limits


async def test_a_product_proposal_still_uses_the_strategist() -> None:
    state, roles = await execute([critique("PASS")])
    assert state["status"] == RunStatus.COMPLETED
    assert len(roles["strategist"].calls) == 1


async def test_unreachable_evidence_is_a_limit_and_the_research_goes_on() -> None:
    """E1: private evidence out of reach is no reason to refuse the public half.
    The critic is told not to demand it; the answer is told to say it is missing."""
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [UNREACHABLE_PLAN]
    state = await run_analysis(build_graph(agent_set, 3), start())
    assert state["stopped_reason"] is None
    assert len(roles["researcher"].calls) == 1
    assert "Internal onboarding records" in roles["critic"].inputs[0]
    assert "Internal onboarding records" in roles["chief"].inputs[0]
    assert "could not" in roles["chief"].inputs[0].lower()


NEEDS_OWNER_PLAN = PLAN | {
    "owner_questions": ["Which six industries do you mean?"],
    "needs_owner_first": True,
}


async def test_a_plan_that_needs_the_owner_first_stops_with_its_questions() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [NEEDS_OWNER_PLAN]
    state = await run_analysis(build_graph(agent_set, 3), start())
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "needs_owner_answers"
    assert roles["researcher"].calls == []
    assert state["synthesis"] is None


async def test_questions_that_would_only_help_do_not_stop_the_task() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [PLAN | {"owner_questions": ["What is your budget?"]}]
    state = await run_analysis(build_graph(agent_set, 3), start())
    assert state["stopped_reason"] is None
    assert len(roles["researcher"].calls) == 1
    assert "What is your budget?" in roles["chief"].inputs[0]


def test_needing_the_owner_first_without_a_question_is_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="owner"):
        TaskPlan.model_validate(PLAN | {"needs_owner_first": True})


def test_the_planner_is_not_told_the_web_is_all_it_has() -> None:
    from app.agents.planner import PlannerAgent

    said = PlannerAgent.instructions
    assert "only research tools available" not in said
    assert "owner_questions" in said and "needs_owner_first" in said
    assert "local" in said.lower() and "tools" in said.lower()


async def test_a_reachable_objective_proceeds_normally() -> None:
    state, roles = await execute([critique("PASS")])
    assert state["stopped_reason"] is None
    assert len(roles["researcher"].calls) == 1


async def test_the_critic_is_told_which_contract_applies() -> None:
    _, roles = await execute([critique("PASS")])
    assert "product_proposal" in roles["critic"].inputs[0]


def test_a_plan_must_name_its_task_kind() -> None:
    from pydantic import ValidationError

    from app.schemas import TaskPlan

    with pytest.raises(ValidationError):
        TaskPlan.model_validate({k: v for k, v in PLAN.items() if k != "task_kind"})


def test_an_unreachable_plan_must_say_what_is_missing() -> None:
    from pydantic import ValidationError

    from app.schemas import TaskPlan

    with pytest.raises(ValidationError, match="Unreachable evidence must be named"):
        TaskPlan.model_validate(PLAN | {"evidence_reachable": False})


def test_the_summary_contract_has_no_market_fields() -> None:
    """The point of the contract is what it does not demand."""
    from app.schemas import ResearchSummary

    fields = set(ResearchSummary.model_fields)
    assert not fields & {"icp", "jtbd", "willingness_to_change", "mvp_scope"}
    assert {"findings", "limits", "trade_offs"} <= fields


async def research_only(
    critiques: list[dict[str, Any]], max_revisions: int = 3
) -> tuple[Any, dict[str, Role]]:
    from app.orchestrator.graph import RESEARCH_WORKFLOW

    agent_set, roles = agents(critiques)
    graph = build_graph(agent_set, max_revisions, workflow=RESEARCH_WORKFLOW)
    return await run_analysis(graph, start()), roles


async def test_a_research_task_skips_strategy_and_is_critiqued_on_the_research() -> None:
    state, roles = await research_only([critique("PASS")])
    assert state["status"] == RunStatus.COMPLETED
    assert roles["strategist"].calls == []
    assert [len(roles[n].calls) for n in ("planner", "researcher", "critic", "chief")] == [
        1,
        1,
        1,
        1,
    ]
    assert "no strategy step" in roles["critic"].inputs[0]


async def test_a_strategy_revision_in_a_research_task_revises_the_research() -> None:
    state, roles = await research_only([critique("REVISE", "strategy"), critique("PASS")])
    assert len(roles["researcher"].calls) == 2 and roles["strategist"].calls == []
    assert state["status"] == RunStatus.COMPLETED


async def test_a_research_task_ends_without_approval_even_with_a_gate() -> None:
    from app.orchestrator.graph import RESEARCH_WORKFLOW

    agent_set, _ = agents([critique("PASS")])
    graph = build_graph(agent_set, 3, gate=object(), workflow=RESEARCH_WORKFLOW)  # type: ignore[arg-type]
    state = await run_analysis(graph, start())
    # The approval step exists for builds; a research answer never reaches it.
    assert state["status"] == RunStatus.COMPLETED
    assert state.get("approval_id") is None


def test_an_unknown_workflow_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown workflow"):
        build_graph(agents([])[0], 3, workflow="freestyle")
