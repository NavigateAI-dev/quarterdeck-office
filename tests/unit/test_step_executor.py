"""D1: the bounded executor for a step plan, its progress check and replans."""

from dataclasses import replace
from typing import Any

import pytest

from app.agents.base import AgentOutputError
from app.orchestrator.graph import RunFailure, run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import RunStatus
from tests.unit.test_graph import PLAN, RESEARCH, STRATEGY, agents, critique, start

EMPTY = RESEARCH | {
    "answers": [{"question": "q", "status": "unanswered", "answer": "Nothing found."}]
}


def step(id_: str, kind: str = "research", depends_on: list[str] | None = None) -> dict:
    return {"id": id_, "kind": kind, "brief": f"do {id_}", "depends_on": depends_on or []}


def planned(*steps: dict) -> dict:
    return PLAN | {"task_kind": "research_summary", "steps": list(steps)}


async def run(
    plans: list[dict],
    critiques: list[dict[str, Any]],
    research: list[dict] | None = None,
    **kw: Any,
) -> tuple[Any, dict]:
    agent_set, roles = agents(critiques)
    roles["planner"].outputs = plans
    if research is not None:
        roles["researcher"].outputs = research
    revisions = kw.pop("max_revisions", 3)
    state = await run_analysis(build_graph(agent_set, revisions, **kw), start())
    return state, roles


def asked(role: Any) -> list[str]:
    return list(role.calls)


async def test_independent_steps_run_together_and_a_dependent_one_after() -> None:
    state, roles = await run(
        [planned(step("a"), step("b"), step("w", "write", ["a", "b"]))], [critique("PASS")]
    )
    assert sorted(asked(roles["researcher"])) == ["do a", "do b"]
    assert asked(roles["strategist"]) == ["do w"]
    assert state["waves"] == 2
    assert state["step_status"] == {"a": "done", "b": "done", "w": "done"}
    assert state["status"] == RunStatus.COMPLETED


async def test_a_write_step_is_given_what_its_dependencies_found() -> None:
    _, roles = await run([planned(step("a"), step("w", "write", ["a"]))], [critique("PASS")])
    given = roles["strategist"].inputs[0]
    assert "do a" in given and '"key_findings"' in given


async def test_a_step_that_found_nothing_is_replanned_keeping_what_stands() -> None:
    first = planned(step("a"), step("b"), step("w", "write", ["a", "b"]))
    second = planned(step("a"), step("b2"), step("w", "write", ["a", "b2"]))
    state, roles = await run(
        [first, second], [critique("PASS")], research=[RESEARCH, EMPTY, RESEARCH]
    )
    assert len(roles["planner"].calls) == 2
    assert asked(roles["researcher"]) == ["do a", "do b", "do b2"], "a is not redone"
    assert state["replans"] == 1
    assert "b" not in state["step_status"] and state["step_status"]["b2"] == "done"


async def test_replans_stop_at_two() -> None:
    """Two replans at most, each after a wave that got somewhere."""
    plans = [
        planned(step("a"), step("b")),
        planned(step("a"), step("b2")),
        planned(step("a"), step("b3")),
        planned(step("a"), step("b4")),
    ]
    research = [RESEARCH, EMPTY, PARTIAL, EMPTY, PARTIAL, EMPTY]
    state, roles = await run(plans, [critique("PASS")], research=research)
    assert state["replans"] <= 2
    assert len(roles["critic"].calls) == 1, "then the work goes to review"


async def test_research_that_keeps_finding_nothing_is_not_replanned_again() -> None:
    """Live, 3 Oct: a hiking route whose sources all failed was replanned after
    every empty wave (each replan reset the count of waves without progress),
    and asked four times for more budget without writing a word."""
    plans = [planned(step("a"), step("w", "write", ["a"])), planned(step("a2"))] + [
        planned(step(f"a{n}")) for n in range(3, 6)
    ]
    state, roles = await run(plans, [critique("PASS")], research=[EMPTY])
    assert len(roles["planner"].calls) == 2, "the plan and one replan, not one per empty wave"
    assert state["replans"] == 1


async def test_when_research_has_failed_the_writer_answers_with_what_there_is() -> None:
    first = planned(step("a"), step("b"), step("w", "write", ["a", "b"]))
    again = planned(step("a2"), step("b2"), step("w", "write", ["a2", "b2"]))
    state, roles = await run([first, again], [critique("PASS")], research=[EMPTY])
    assert asked(roles["strategist"]) == ["do w"], "an answer that states its gaps, not none"
    assert state["step_status"]["w"] == "done"
    assert state["status"] == RunStatus.COMPLETED


async def test_a_failed_step_costs_that_step_not_the_task() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("a"), step("b")), planned(step("a"), step("c"))]

    class Flaky:
        role = "researcher"
        calls: list[str] = []

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.schemas import ResearchReport

            self.calls.append(objective)
            if objective == "do b":
                raise AgentOutputError("researcher", "no_search_results")
            return ResearchReport.model_validate(RESEARCH)

    flaky = Flaky()
    agent_set = replace(agent_set, researcher=flaky)  # type: ignore[arg-type]
    state = await run_analysis(build_graph(agent_set, 3), start())
    assert state["status"] == RunStatus.COMPLETED
    assert flaky.calls.count("do a") == 1 and "do c" in flaky.calls


async def test_a_budget_stop_inside_a_step_still_stops_the_task() -> None:
    agent_set, _ = agents([critique("PASS")])
    agent_set.planner.outputs = [planned(step("a"))]  # type: ignore[attr-defined]

    class Spent:
        role = "researcher"

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise AgentOutputError("researcher", "budget_exhausted:cost")

    agent_set = replace(agent_set, researcher=Spent())  # type: ignore[arg-type]
    with pytest.raises(RunFailure) as stopped:
        await run_analysis(build_graph(agent_set, 3), start())
    assert stopped.value.code == "budget_exhausted:cost"


async def test_a_revision_adds_a_targeted_step_and_reruns_only_what_depends_on_it() -> None:
    first = planned(step("a"), step("w", "write", ["a"]))
    second = planned(step("a"), step("c"), step("w", "write", ["a", "c"]))
    state, roles = await run([first, second], [critique("REVISE", "research"), critique("PASS")])
    assert asked(roles["researcher"]) == ["do a", "do c"]
    assert asked(roles["strategist"]) == ["do w", "do w"], "the writer uses the new finding"
    assert state["revision_count"] == 1
    assert state["status"] == RunStatus.COMPLETED


async def test_a_revision_the_replan_does_nothing_about_stops_as_no_progress() -> None:
    same = planned(step("a"), step("w", "write", ["a"]))
    state, _ = await run([same, same], [critique("REVISE", "research"), critique("PASS")])
    assert state["stopped_reason"] == "revision_made_no_change"
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW


async def test_a_research_bot_plan_drops_writing() -> None:
    from app.orchestrator.graph import RESEARCH_WORKFLOW

    state, roles = await run(
        [planned(step("a"), step("w", "write", ["a"]))],
        [critique("PASS")],
        workflow=RESEARCH_WORKFLOW,
    )
    assert asked(roles["strategist"]) == [] and asked(roles["researcher"]) == ["do a"]
    assert "w" not in state["step_status"]


async def test_a_plan_without_steps_runs_the_fixed_pipeline() -> None:
    state, roles = await run([PLAN], [critique("PASS")])
    assert len(roles["researcher"].calls) == 1 and len(roles["strategist"].calls) == 1
    assert state.get("step_status") in (None, {})


PARTIAL = RESEARCH | {
    "answers": [
        {"question": "price", "status": "partial", "answer": "Only Sumsub publishes prices."}
    ]
}


async def test_a_partial_answer_is_progress_not_a_reason_to_replan() -> None:
    """Live finding: unpublished prices made every step 'found nothing', and the
    task replanned twice, redoing all its research each time."""
    state, roles = await run(
        [planned(step("a"), step("b"), step("w", "write", ["a", "b"]))],
        [critique("PASS")],
        research=[PARTIAL, PARTIAL],
    )
    assert len(roles["planner"].calls) == 1
    assert state["step_status"] == {"a": "done", "b": "done", "w": "done"}


async def test_a_step_that_answered_nothing_at_all_is_still_replanned() -> None:
    _, roles = await run(
        [planned(step("a")), planned(step("a2"))], [critique("PASS")], research=[EMPTY, RESEARCH]
    )
    assert len(roles["planner"].calls) == 2


# The Chief attaches skills to steps.


class Instructed:
    """A role that records the instructions it was given, copies included."""

    def __init__(self, role: str, schema: type, output: dict, seen: list[tuple[str, str]]) -> None:
        self.role = role
        self.schema = schema
        self.output = output
        self.seen = seen
        self.instructions = f"base {role}"

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        self.seen.append((objective, self.instructions))
        return self.schema.model_validate(self.output)


SKILLS = {
    "to-questionnaire": {
        "name": "to-questionnaire",
        "description": "Turn a gap into a questionnaire.",
        "instructions": "QUESTIONNAIRE RULES",
        "roles": ["summarist", "strategist"],
        "source": "acme/skills",
    },
    "seo-audit": {
        "name": "seo-audit",
        "description": "Audit a site's SEO.",
        "instructions": "SEO RULES",
        "roles": ["researcher"],
        "source": "acme/marketing",
    },
}


def with_skills(plan: dict) -> tuple[Any, list, dict]:
    from app.schemas import ResearchReport, StrategyProposal

    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [plan]
    seen: list[tuple[str, str]] = []
    agent_set = replace(
        agent_set,
        researcher=Instructed("researcher", ResearchReport, RESEARCH, seen),  # type: ignore[arg-type]
        strategist=Instructed("strategist", StrategyProposal, STRATEGY, seen),  # type: ignore[arg-type]
        skills=SKILLS,
    )
    return agent_set, seen, roles


async def test_a_step_with_a_skill_gives_its_worker_the_skill_and_no_one_else() -> None:
    plan = PLAN | {
        "steps": [
            {
                "id": "a",
                "kind": "research",
                "brief": "do a",
                "depends_on": [],
                "skill": "seo-audit",
            },
            {"id": "b", "kind": "research", "brief": "do b", "depends_on": []},
            {
                "id": "w",
                "kind": "write",
                "brief": "do w",
                "depends_on": ["a", "b"],
                "skill": "to-questionnaire",
            },
        ]
    }
    agent_set, seen, roles = with_skills(plan)
    state = await run_analysis(build_graph(agent_set, 3), start())
    told = dict(seen)
    assert "SEO RULES" in told["do a"] and "SEO RULES" not in told["do b"]
    assert "QUESTIONNAIRE RULES" in told["do w"]
    assert state["step_skills"] == {"a": "applied", "w": "applied"}
    assert "seo-audit" in roles["planner"].inputs[0], "the Chief is shown what it may attach"


async def test_a_skill_the_step_cannot_use_is_left_off_and_said_so() -> None:
    plan = PLAN | {
        "steps": [
            {"id": "a", "kind": "research", "brief": "do a", "depends_on": [], "skill": "ghost"},
            {
                "id": "b",
                "kind": "research",
                "brief": "do b",
                "depends_on": [],
                "skill": "to-questionnaire",
            },
        ]
    }
    agent_set, seen, _ = with_skills(plan)
    state = await run_analysis(build_graph(agent_set, 3), start())
    assert all("RULES" not in told for _, told in seen)
    assert state["step_skills"] == {"a": "unknown", "b": "wrong_role"}


async def test_once_the_answer_is_written_a_failed_step_is_not_replanned() -> None:
    """Live, 4 Oct: the answer was written, then a research step that had found
    nothing earlier sent the task back to the Chief, and it researched and
    wrote again."""
    state, roles = await run(
        [planned(step("a"), step("b"), step("w", "write", ["a"])), planned(step("x"))],
        [critique("PASS")],
        research=[RESEARCH, EMPTY],
    )
    assert len(roles["planner"].calls) == 1
    assert asked(roles["strategist"]) == ["do w"]
    assert state["status"] == RunStatus.COMPLETED


async def test_with_no_revisions_allowed_a_revise_stops_for_the_owner_with_the_answer() -> None:
    """Work started from a discussion asks before it spends more on revising."""
    state, roles = await run(
        [planned(step("a"), step("w", "write", ["a"]))],
        [critique("REVISE", "research")],
        max_revisions=0,
    )
    assert len(roles["planner"].calls) == 1, "no replan for the revision"
    assert asked(roles["strategist"]) == ["do w"]
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "revision_limit_reached"


# 6 October: the gateway was down and the backup rate-limited; every research
# step failed, the task replanned, failed again, and stopped for review showing
# the critic's concerns. Clicking again could not help, and nothing said so.


def failing_researcher(code: str, succeed: set[str] | None = None) -> Any:
    class Down:
        role = "researcher"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            from app.schemas import ResearchReport

            self.calls.append(objective)
            if succeed and objective in succeed:
                return ResearchReport.model_validate(RESEARCH)
            raise AgentOutputError("researcher", code)

    return Down()


async def test_research_that_all_fails_on_providers_stops_at_once_and_says_why() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("a"), step("b"), step("w", "write", ["a", "b"]))]
    down = failing_researcher("rate_limited")
    agent_set = replace(agent_set, researcher=down)  # type: ignore[arg-type]
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["stopped_reason"] == "providers_unavailable"
    assert state["provider_failure"]["codes"] == ["rate_limited"]
    assert len(roles["planner"].calls) == 1, "no replan: the providers are the problem"
    assert roles["critic"].calls == [], "no review of work that never ran"


async def test_one_provider_failure_beside_finished_research_carries_on() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [
        planned(step("a"), step("b"), step("w", "write", ["a", "b"])),
        planned(step("a"), step("b2"), step("w", "write", ["a", "b2"])),
    ]
    down = failing_researcher("provider_unavailable", succeed={"do a", "do b2"})
    agent_set = replace(agent_set, researcher=down)  # type: ignore[arg-type]
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert state.get("stopped_reason") != "providers_unavailable"
    assert state["status"] == RunStatus.COMPLETED


async def test_research_failing_for_other_reasons_is_not_called_a_provider_outage() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("a"))] * 4
    down = failing_researcher("no_search_results")
    agent_set = replace(agent_set, researcher=down)  # type: ignore[arg-type]
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert state.get("stopped_reason") != "providers_unavailable"


def test_an_answer_from_the_notes_keeps_what_was_found_and_names_what_was_not() -> None:
    """6 Oct: finishing must never turn paid-for work into nothing."""
    from app.orchestrator.graph import as_found
    from app.schemas.agent_output import Synthesis

    plan = PLAN | {
        "steps": [
            {"id": "a", "kind": "research", "brief": "do a", "depends_on": []},
            {"id": "b", "kind": "research", "brief": "do b", "depends_on": []},
        ]
    }
    state = {"plan": plan, "research": RESEARCH, "step_status": {"a": "done", "b": "failed"}}
    answer = as_found(state)  # type: ignore[arg-type]
    Synthesis.model_validate(answer)
    assert answer["facts"] == RESEARCH["key_findings"]
    assert "Not done before the task was finished: do b" in answer["unknowns"]
    assert answer["proposes_action"] is False


class FakeBrowser:
    """The office's browser agent, as a task's work meets it."""

    role = "researcher"

    def __init__(self, stuck: bool = False) -> None:
        self.briefs: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self.stuck = stuck

    async def browse(
        self,
        brief: str,
        context: Any,
        effort: str = "standard",
        notes: str = "",
        step_id: str = "",
    ) -> dict[str, Any]:
        from app.browser.agent import BrowserMove, as_research

        self.briefs.append((brief, effort))
        self.notes.append(notes)
        if self.stuck:
            report = as_research(brief, None, [])
            report["stuck"] = {"what": "click 'Case studies' on example.com", "tries": 3}
            return report
        move = BrowserMove(
            action="done",
            answer="Rooms from EUR 120.",
            findings=["Hotel X lists EUR 120 (booking page)"],
        )
        return as_research(brief, move, [{"url": "https://example.com/rooms", "title": "Rooms"}])


async def test_a_browse_step_uses_the_office_browser_and_feeds_the_writing() -> None:
    """CU2, 8 Oct: a browse step's findings reach the write step like research."""
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("b", "browse"), step("w", "write", ["b"]))]
    browser = FakeBrowser()
    agent_set = replace(agent_set, browser=browser)
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert state["status"] == RunStatus.COMPLETED
    assert browser.briefs and browser.briefs[0][1] == "standard"
    assert roles["researcher"].calls == [], "the browser did the step, not page reading"
    assert "EUR 120" in roles["strategist"].inputs[-1] or "EUR 120" in str(state.get("research"))


async def test_with_the_browser_off_a_browse_step_is_read_as_research() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("b", "browse"), step("w", "write", ["b"]))]
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert state["status"] == RunStatus.COMPLETED
    assert len(roles["researcher"].calls) == 1


async def test_a_stuck_browse_step_waits_for_the_owner_and_says_why() -> None:
    """OI2: the wave is kept, the step waits, and what stuck is handed up."""
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("b", "browse"), step("w", "write", ["b"]))]
    agent_set = replace(agent_set, browser=FakeBrowser(stuck=True))
    seen: list[dict[str, Any]] = []

    class Paused(Exception):
        pass

    def on_node(node: str, values: dict[str, Any]) -> None:
        if values.get("stuck"):
            seen.append(values)
            raise Paused

    with pytest.raises(Paused):
        await run_analysis(build_graph(agent_set, 0), start(), on_node=on_node)
    assert seen[0]["stuck"]["step"] == "b"
    assert "Case studies" in seen[0]["stuck"]["what"]
    assert seen[0]["step_status"]["b"] == "pending"
    assert roles["strategist"].inputs == [], "nothing was written on a stuck step"


async def test_a_step_the_owner_skipped_is_not_run() -> None:
    """OI4: skipped, the answer is written with what there is."""
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("b", "browse"), step("w", "write", ["b"]))]
    browser = FakeBrowser()
    agent_set = replace(agent_set, browser=browser)
    state = await run_analysis(build_graph(agent_set, 0, skipped=lambda: {"b"}), start())
    assert browser.briefs == []
    assert state["step_status"]["b"] == "empty"


async def test_the_owners_notes_go_with_a_browse_step() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("b", "browse"), step("w", "write", ["b"]))]
    browser = FakeBrowser()
    agent_set = replace(agent_set, browser=browser)
    graph = build_graph(agent_set, 0, guidance=lambda node: ["Use the sitemap."])
    await run_analysis(graph, start())
    assert "Use the sitemap." in browser.notes[0]


# The plan is reviewed before any work starts.


def reviewer(*verdicts: dict[str, Any]) -> Any:
    from app.schemas.agent_output import PlanReview
    from tests.unit.test_graph import Role

    return Role("critic", PlanReview, list(verdicts))


CHANGE = {
    "verdict": "change",
    "changes": [{"change": "Search in Dutch too", "why": "Lyon's classes list in French"}],
    "plain": "Misses Dutch sources.",
}
READY = {"verdict": "ready", "changes": [], "plain": "Small and to the point."}


async def test_a_plan_the_review_finds_ready_runs_as_drafted() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("a"), step("w", "write", ["a"]))]
    review = reviewer(READY)
    agent_set = replace(agent_set, plan_reviewer=review)
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert len(review.calls) == 1
    assert [s["id"] for s in state["plan"]["steps"]] == ["a", "w"]
    assert state["status"] == RunStatus.COMPLETED


async def test_a_challenged_plan_is_revised_before_any_work() -> None:
    agent_set, roles = agents([critique("PASS")])
    first = planned(step("a"), step("w", "write", ["a"]))
    revised = planned(step("a"), step("nl"), step("w", "write", ["a", "nl"]))
    roles["planner"].outputs = [first, revised]
    review = reviewer(CHANGE, READY)
    agent_set = replace(agent_set, plan_reviewer=review)
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert "Search in Dutch too" in roles["planner"].inputs[1]
    assert [s["id"] for s in state["plan"]["steps"]] == ["a", "nl", "w"]
    assert len(review.calls) == 2
    assert roles["researcher"].calls, "work started after the review"


async def test_the_review_stops_after_two_rounds() -> None:
    agent_set, roles = agents([critique("PASS")])
    plans = [
        planned(step("a"), step("w", "write", ["a"])),
        planned(step("b"), step("w", "write", ["b"])),
        planned(step("c"), step("w", "write", ["c"])),
    ]
    roles["planner"].outputs = plans + [plans[-1]]
    review = reviewer(CHANGE, CHANGE, CHANGE)
    agent_set = replace(agent_set, plan_reviewer=review)
    state = await run_analysis(build_graph(agent_set, 0), start())
    assert len(review.calls) == 2
    assert [s["id"] for s in state["plan"]["steps"]] == ["c", "w"]


async def test_a_revision_that_changes_nothing_ends_the_review() -> None:
    agent_set, roles = agents([critique("PASS")])
    same = planned(step("a"), step("w", "write", ["a"]))
    roles["planner"].outputs = [same, same]
    review = reviewer(CHANGE, CHANGE)
    agent_set = replace(agent_set, plan_reviewer=review)
    await run_analysis(build_graph(agent_set, 0), start())
    assert len(review.calls) == 1


def test_a_plan_diff_names_what_moved() -> None:
    from app.orchestrator.graph import plan_diff
    from app.schemas import TaskPlan

    before = TaskPlan.model_validate(planned(step("a"), step("w", "write", ["a"])))
    after = TaskPlan.model_validate(planned(step("b"), step("w", "write", ["b"])))
    assert plan_diff(before, after) == "1 added, 1 dropped, 1 rewritten"
    assert plan_diff(before, before) == ""


async def test_a_step_the_owner_wants_next_runs_first() -> None:
    """13d: with room for one step at a time, the owner's "Do next" goes first;
    the rest keep the plan's order."""
    state, roles = await run(
        [planned(step("a"), step("b"), step("c"))],
        [critique("PASS")],
        max_parallel=1,
        prioritized=lambda: ["c"],
    )
    order = [call for call in asked(roles["researcher"]) if call.startswith("do ")]
    assert order[:3] == ["do c", "do a", "do b"], order
    assert state["status"] == RunStatus.COMPLETED
