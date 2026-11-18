"""D1: the Chief's step plan — a small DAG of typed steps, bounded in code."""

import pytest
from pydantic import ValidationError

from app.schemas import TaskPlan

PLAN = {
    "objective": "o",
    "task_kind": "research_summary",
    "research_question": "Which KYC vendors suit a Dutch EMI?",
    "acceptance_criteria": ["c"],
    "expected_artifacts": ["report"],
}


def step(id_: str, kind: str = "research", depends_on: list[str] | None = None) -> dict:
    return {"id": id_, "kind": kind, "brief": f"do {id_}", "depends_on": depends_on or []}


def test_a_dag_of_typed_steps_is_accepted() -> None:
    plan = TaskPlan.model_validate(
        PLAN
        | {
            "steps": [
                step("vendors"),
                step("rules"),
                step("compare", "write", ["vendors", "rules"]),
            ]
        }
    )
    assert [s.id for s in plan.steps] == ["vendors", "rules", "compare"]
    assert plan.steps[0].effort == "standard"


def test_a_plan_without_steps_is_the_fixed_pipeline() -> None:
    assert TaskPlan.model_validate(PLAN).steps == []


@pytest.mark.parametrize(
    "steps, reason",
    [
        ([step("a", depends_on=["b"]), step("b", depends_on=["a"])], "cycle"),
        ([step("a", depends_on=["ghost"])], "unknown"),
        ([step("a"), step("a")], "twice"),
        ([step("a", kind="dance")], "kind"),
        ([step(f"s{i}") for i in range(9)], "at most"),
        ([step("a", depends_on=["a"])], "cycle"),
    ],
)
def test_a_plan_the_executor_could_not_run_is_refused(steps: list, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        TaskPlan.model_validate(PLAN | {"steps": steps})


def test_steps_in_the_order_they_can_run() -> None:
    plan = TaskPlan.model_validate(
        PLAN | {"steps": [step("w", "write", ["b"]), step("b", depends_on=["a"]), step("a")]}
    )
    assert plan.waves() == [["a"], ["b"], ["w"]]
