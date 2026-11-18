"""Parallel subtasks, owner guidance, and stopping between steps (slice 4)."""

import asyncio
from typing import Any

import pytest

from app.orchestrator.graph import RunStopped, run_analysis
from app.orchestrator.pipeline import build_graph
from app.schemas import ResearchReport
from tests.unit.test_graph import PLAN, RESEARCH, Role, agents, critique, start


class SlowResearcher(Role):
    """Records how many copies run at once."""

    active = 0
    peak = 0

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        SlowResearcher.active += 1
        SlowResearcher.peak = max(SlowResearcher.peak, SlowResearcher.active)
        await asyncio.sleep(0.05)
        SlowResearcher.active -= 1
        self.calls.append(objective)
        self.inputs.append(inputs)
        url = f"https://example.com/{len(self.calls)}"
        return ResearchReport.model_validate(
            RESEARCH
            | {
                "key_findings": [f"finding for {objective}"],
                "sources": [{"title": objective, "url": url, "source_type": "primary"}],
            }
        )


def with_subquestions(*questions: str) -> tuple[Any, dict[str, Role]]:
    set_, roles = agents([critique("PASS")])
    roles["planner"].outputs = [PLAN | {"subquestions": list(questions)}]
    slow = SlowResearcher("researcher", ResearchReport, [RESEARCH])
    SlowResearcher.active = SlowResearcher.peak = 0
    object.__setattr__(set_, "researcher", slow)
    roles["researcher"] = slow
    return set_, roles


async def test_independent_subquestions_are_researched_in_parallel() -> None:
    set_, roles = with_subquestions("EU pricing", "US pricing")
    state = await run_analysis(build_graph(set_, 3, max_parallel=4), start())
    assert sorted(roles["researcher"].calls) == ["EU pricing", "US pricing"]
    assert SlowResearcher.peak == 2
    assert len(roles["critic"].calls) == 1  # the dependent review runs once
    assert [t["question"] for t in state["research_subtasks"]] == ["EU pricing", "US pricing"]
    merged = state["research"]
    assert {s["url"] for s in merged["sources"]} == {
        "https://example.com/1",
        "https://example.com/2",
    }
    assert any("EU pricing" in f for f in merged["key_findings"])


async def test_concurrency_is_capped() -> None:
    set_, _ = with_subquestions("a", "b", "c", "d")
    await run_analysis(build_graph(set_, 3, max_parallel=2), start())
    assert SlowResearcher.peak == 2


async def test_a_single_question_is_researched_once_as_before() -> None:
    set_, roles = agents([critique("PASS")])
    state = await run_analysis(build_graph(set_, 3), start())
    assert len(roles["researcher"].calls) == 1
    assert state["research_subtasks"] == []


async def test_owner_guidance_reaches_every_step() -> None:
    set_, roles = agents([critique("PASS")])
    seen: list[str] = []

    def guidance(node: str) -> list[str]:
        seen.append(node)
        return ["Focus on Europe"]

    await run_analysis(build_graph(set_, 3, guidance=guidance), start())
    assert seen == ["plan", "research", "strategy", "critique", "synthesis"]
    for role in ("planner", "researcher", "strategist", "critic", "chief"):
        assert "Focus on Europe" in roles[role].inputs[0]


async def test_stopping_at_a_step_boundary_starts_no_further_step() -> None:
    set_, roles = agents([critique("PASS")])

    def stop_after_plan(node: str, values: dict[str, Any]) -> None:
        if node == "plan":
            raise RunStopped("paused")

    with pytest.raises(RunStopped):
        await run_analysis(build_graph(set_, 3), start(), on_node=stop_after_plan)
    assert roles["researcher"].calls == []


async def test_the_plans_official_pages_reach_every_research_subtask() -> None:
    set_, roles = with_subquestions("EU pricing", "US pricing")
    roles["planner"].outputs = [
        PLAN
        | {
            "subquestions": ["EU pricing", "US pricing"],
            "official_sources": ["https://a.example/p"],
        }
    ]
    seen: list[Any] = []
    original = SlowResearcher.run

    async def capture(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(getattr(self, "official_urls", None))
        return await original(self, *args, **kwargs)

    SlowResearcher.run = capture  # type: ignore[method-assign]
    try:
        await run_analysis(build_graph(set_, 3), start())
    finally:
        SlowResearcher.run = original  # type: ignore[method-assign]
    assert seen == [["https://a.example/p"], ["https://a.example/p"]]
