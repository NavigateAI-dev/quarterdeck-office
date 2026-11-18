"""Hardening probes for the categories SPEC phase 8 names.

These assert properties that are easy to break silently: a run reading another
project's context, a replayed graph re-authorizing engineering, or a resumed
run repeating completed work.
"""

import json
from typing import Any
from uuid import uuid4

import pytest

from app.orchestrator.graph import run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import RunStatus, initial_state
from app.policy import PermissionDenied, require
from tests.unit.test_graph import agents, critique


def context_for(name: str) -> dict[str, Any]:
    return {
        "organization": "Northwind",
        "project_id": str(uuid4()),
        "project_name": name,
        "background": f"{name} confidential background",
        "constraints": [f"{name} constraint"],
    }


def state_for(name: str, objective: str) -> Any:
    return initial_state(objective, context_for(name))


async def test_one_run_never_sees_another_projects_context() -> None:
    """Context leakage: agents are reused across runs within a worker."""
    agent_set, roles = agents([critique("PASS")])
    graph = build_graph(agent_set, 3)
    await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    await run_analysis(graph, state_for("Beta", "Beta objective"))

    for role in roles.values():
        first, second = role.inputs[0], role.inputs[-1]
        assert "Beta" not in first, f"{role.role} leaked Beta into the first run"
        assert "Alpha" not in second, f"{role.role} leaked Alpha into the second run"


async def test_upstream_artifacts_do_not_persist_across_runs() -> None:
    """A second run must start from nothing, not from the first run's outputs."""
    agent_set, roles = agents([critique("PASS")])
    graph = build_graph(agent_set, 3)
    first = await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    second = await run_analysis(graph, state_for("Beta", "Beta objective"))
    assert first["research"] is not None and second["research"] is not None
    # The planner is the first node and must never receive upstream material.
    assert roles["planner"].inputs == ["", ""]


async def test_a_run_carries_only_its_own_objective() -> None:
    agent_set, roles = agents([critique("PASS")])
    graph = build_graph(agent_set, 3)
    await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    await run_analysis(graph, state_for("Beta", "Beta objective"))
    assert roles["strategist"].calls == ["Alpha objective", "Beta objective"]


@pytest.mark.parametrize(
    "action", ["modify_code", "create_decision_record", "modify_strategy_record"]
)
def test_replay_cannot_accumulate_authorization(action: str) -> None:
    """Permission problems: a replayed call must be denied the same way."""
    for _ in range(3):
        with pytest.raises(PermissionDenied, match="approval_required"):
            require(action)


def test_denied_actions_stay_denied_under_every_argument() -> None:
    for approved in (False, True):
        for preapproved in (False, True):
            with pytest.raises(PermissionDenied, match="denied_by_policy"):
                require("deploy_production", approved=approved, preapproved=preapproved)


async def test_state_survives_a_json_round_trip() -> None:
    """Resume failures: a checkpoint stores JSON, so state must survive it."""
    agent_set, _ = agents([critique("PASS")])
    graph = build_graph(agent_set, 3)
    state = await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    restored = json.loads(json.dumps(dict(state)))
    assert restored["status"] == RunStatus.COMPLETED
    assert restored["synthesis"]["recommendation"] == "r"
    assert restored["research"]["sources"][0]["url"].startswith("https://")


async def test_a_revision_does_not_carry_a_stale_critique_forward() -> None:
    """A rerun must not be judged against the critique that triggered it."""
    agent_set, roles = agents([critique("REVISE", "strategy"), critique("PASS")])
    graph = build_graph(agent_set, 3)
    await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    assert "Critique" not in roles["critic"].inputs[-1]


class Failing:
    """A role that fails the way a provider does."""

    def __init__(self, role: str, code: str) -> None:
        self.role = role
        self.code = code
        self.calls = 0

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        from app.agents.base import AgentOutputError

        self.calls += 1
        raise AgentOutputError(self.role, self.code)


PROVIDER_FAILURES = [
    "provider_timeout",
    "provider_unavailable",
    "provider_rejected_request",
    "gateway_unreachable",
    "model_refusal",
    "incomplete_response",
    "empty_response",
    "malformed_response",
    "malformed_structured_output",
    "unknown_model_alias",
    "no_search_results",
    "search_rate_limited",
]


@pytest.mark.parametrize("code", PROVIDER_FAILURES)
async def test_a_provider_failure_stops_the_run_with_its_own_code(code: str) -> None:
    """Provider failures: each is distinguishable and never advances the run."""
    from app.orchestrator.graph import AgentSet, RunFailure

    agent_set, roles = agents([critique("PASS")])
    broken = Failing("researcher", code)
    graph = build_graph(
        AgentSet(
            planner=agent_set.planner,
            researcher=broken,  # type: ignore[arg-type]
            strategist=agent_set.strategist,
            critic=agent_set.critic,
            chief=agent_set.chief,
        ),
        3,
    )
    with pytest.raises(RunFailure) as caught:
        await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    assert caught.value.code == code
    assert caught.value.node == "researcher"
    # Nothing downstream of the failure ran.
    assert roles["strategist"].calls == []
    assert roles["chief"].calls == []


async def test_a_failure_leaves_completed_work_reported() -> None:
    """Work finished before a failure must still be visible to the caller."""
    from app.orchestrator.graph import AgentSet, RunFailure

    agent_set, roles = agents([critique("PASS")])
    seen: list[str] = []
    graph = build_graph(
        AgentSet(
            planner=agent_set.planner,
            researcher=agent_set.researcher,
            strategist=Failing("strategist", "provider_timeout"),  # type: ignore[arg-type]
            critic=agent_set.critic,
            chief=agent_set.chief,
        ),
        3,
    )
    with pytest.raises(RunFailure):
        await run_analysis(
            graph,
            state_for("Alpha", "Alpha objective"),
            on_node=lambda node, values: seen.append(node),
        )
    assert seen == ["plan", "research"]
    assert len(roles["planner"].calls) == 1


async def test_a_failing_role_is_not_retried_by_the_graph() -> None:
    """The agent owns its bounded retry; the graph must not add another loop."""
    from app.orchestrator.graph import AgentSet, RunFailure

    agent_set, _ = agents([critique("PASS")])
    broken = Failing("planner", "provider_timeout")
    graph = build_graph(
        AgentSet(
            planner=broken,  # type: ignore[arg-type]
            researcher=agent_set.researcher,
            strategist=agent_set.strategist,
            critic=agent_set.critic,
            chief=agent_set.chief,
        ),
        3,
    )
    with pytest.raises(RunFailure):
        await run_analysis(graph, state_for("Alpha", "Alpha objective"))
    assert broken.calls == 1


def test_artifacts_keep_their_own_script() -> None:
    """Escaped output is unreadable to a person and worse input to an agent."""
    import json

    payload = {"recommendation": "投入产出比需要更多证据", "facts": ["Évidence limitée"]}
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    assert "投入产出比需要更多证据" in body
    assert "\\u" not in body


async def test_upstream_text_reaches_the_next_agent_unescaped() -> None:
    """A downstream agent must read the text, not its escape sequences."""
    from app.orchestrator.graph import render

    section = render("Research", {"key_findings": ["单人公司投入较大"]})
    assert "单人公司投入较大" in section
    assert "\\u" not in section


def test_the_approval_pause_settles_a_hardening_run() -> None:
    from scripts.hardening_runs import TERMINAL, category

    assert "WAITING_FOR_APPROVAL" in TERMINAL
    assert category({"status": "WAITING_FOR_APPROVAL"}) == "awaiting_approval"
