import pytest
from pydantic import ValidationError

from app.orchestrator.routing import STOP, revision_budget, route_after_critique, stop_status
from app.orchestrator.state import AnalysisState, Node, RunStatus, initial_state
from app.schemas import Critique


def state_with(verdict: str, target: str | None = None, revisions: int = 0) -> AnalysisState:
    state = initial_state("objective", {"project_name": "p"})
    body: dict[str, object] = {"verdict": verdict, "confidence": "medium"}
    if target is not None:
        body["revision_target"] = target
    state["critique"] = body
    state["revision_count"] = revisions
    return state


def test_pass_advances_to_synthesis() -> None:
    assert route_after_critique(state_with("PASS"), 3) == Node.SYNTHESIS


def test_block_stops_without_synthesis() -> None:
    state = state_with("BLOCK")
    assert route_after_critique(state, 3) == STOP
    assert stop_status(state, 3) == (RunStatus.BLOCKED, "critic_blocked")


@pytest.mark.parametrize(
    "target,expected", [("research", Node.RESEARCH), ("strategy", Node.STRATEGY)]
)
def test_revision_returns_to_its_target(target: str, expected: Node) -> None:
    assert route_after_critique(state_with("REVISE", target), 3) == expected


def test_revision_budget_is_bounded() -> None:
    state = state_with("REVISE", "research", revisions=3)
    assert route_after_critique(state, 3) == STOP
    assert stop_status(state, 3) == (RunStatus.NEEDS_HUMAN_REVIEW, "revision_limit_reached")


def test_final_revision_within_budget_still_routes() -> None:
    assert route_after_critique(state_with("REVISE", "strategy", revisions=2), 3) == Node.STRATEGY


def test_zero_budget_never_revises() -> None:
    assert route_after_critique(state_with("REVISE", "research"), 0) == STOP


def test_revision_without_a_target_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must set revision_target"):
        Critique.model_validate({"verdict": "REVISE", "confidence": "low"})


def test_target_on_a_non_revision_verdict_is_rejected() -> None:
    with pytest.raises(ValidationError, match="only valid for a REVISE"):
        Critique.model_validate(
            {"verdict": "PASS", "revision_target": "research", "confidence": "high"}
        )


def test_unknown_target_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Critique.model_validate(
            {"verdict": "REVISE", "revision_target": "engineering", "confidence": "low"}
        )


# 6 October: work from a discussion has no automatic revisions, and the same
# zero also blocked the revision the owner asked for, so every review choice
# went back to the same stop.


def _revise(**state: object) -> dict:
    return {
        "critique": {"verdict": "REVISE", "revision_target": "research", "confidence": "medium"},
        **state,
    }


def test_the_critic_alone_gets_no_revision_on_discussion_work() -> None:
    assert route_after_critique(_revise(revision_count=0), 0) == STOP


def test_the_owners_revision_runs_even_with_no_automatic_budget() -> None:
    owner_asked = _revise(revision_count=0, human_cycles=1)
    assert revision_budget(owner_asked, 0) == 1
    assert route_after_critique(owner_asked, 0) == Node.RESEARCH


def test_after_the_owners_revision_the_critic_stops_for_them_again() -> None:
    revised = _revise(revision_count=1, human_cycles=1)
    assert route_after_critique(revised, 0) == STOP
    assert stop_status(revised, 0)[1] == "revision_limit_reached"


def test_a_configured_budget_is_never_lowered_by_a_review() -> None:
    assert revision_budget(_revise(human_cycles=1), 2) == 2
