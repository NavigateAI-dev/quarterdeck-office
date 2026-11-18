"""The workflow of one task, drawn from what it recorded.

Every part of the map has to trace to an event or a state key: the map shows
what the office did, not a diagram of what it might do.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.run_flow import build_flow

START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def event(kind: str, seconds: float, **payload: Any) -> dict[str, Any]:
    return {"kind": kind, "payload": payload, "at": START + timedelta(seconds=seconds)}


def step(
    id_: str, kind: str = "research", depends_on: list[str] | None = None, effort: str = "standard"
) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,
        "brief": f"Find out {id_}",
        "effort": effort,
        "depends_on": depends_on or [],
        "skill": None,
    }


PLAN = {
    "steps": [
        step("rules"),
        step("vendors"),
        step("prices"),
        step("report", "write", ["rules", "vendors", "prices"], "deep"),
    ]
}


def flow(
    state: dict[str, Any] | None = None,
    status: str = "RUNNING",
    events: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return build_flow(
        state=state if state is not None else {"plan": PLAN, "step_status": {}},
        status=status,
        workflow="analysis",
        events=events or [],
        usage=extra.get("usage", []),
        line=extra.get("line"),
        max_parallel=extra.get("max_parallel", 4),
        advisor_on=extra.get("advisor_on", False),
        rejections=extra.get("rejections", []),
        room=extra.get("room", []),
        team_on=extra.get("team_on", False),
    )


def test_steps_are_laid_out_by_what_they_wait_for() -> None:
    drawn = flow()
    levels = {card["id"]: card["level"] for card in drawn["steps"]}
    assert levels == {"rules": 1, "vendors": 1, "prices": 1, "report": 2}
    assert [card["role"] for card in drawn["steps"]] == [
        "researcher",
        "researcher",
        "researcher",
        "writer",
    ]


def test_the_steps_that_are_running_are_the_ready_ones_while_work_runs() -> None:
    events = [
        event("step_started", 0, node="plan"),
        event("step_completed", 30, node="plan"),
        event("step_started", 31, node="research"),
    ]
    drawn = flow(events=events, max_parallel=2)
    running = [card["id"] for card in drawn["steps"] if card["status"] == "running"]
    assert running == ["rules", "vendors"], "at most max_parallel, in plan order"
    assert drawn["phase"] == "workers"
    assert drawn["status_line"]["running"] == 2


def test_nothing_runs_when_the_task_is_not_running() -> None:
    events = [event("step_started", 31, node="research")]
    drawn = flow(events=events, status="PAUSED")
    assert all(card["status"] != "running" for card in drawn["steps"])
    assert drawn["active"] is False


def test_finished_steps_keep_their_outcome() -> None:
    state = {
        "plan": PLAN,
        "step_status": {"rules": "done", "vendors": "empty", "prices": "failed"},
    }
    drawn = flow(state=state, status="COMPLETED")
    outcome = {card["id"]: card["status"] for card in drawn["steps"]}
    assert outcome == {
        "rules": "done",
        "vendors": "empty",
        "prices": "failed",
        "report": "pending",
    }


def test_the_chief_is_replanning_when_it_plans_a_second_time() -> None:
    first = [event("step_started", 0, node="plan")]
    assert flow(events=first)["chief"]["doing"] == "planning"
    again = [
        event("step_started", 0, node="plan"),
        event("step_completed", 30, node="plan"),
        event("step_started", 31, node="research"),
        event("step_completed", 60, node="work"),
        event("step_started", 61, node="plan"),
    ]
    drawn = flow(events=again, state={"plan": PLAN, "step_status": {}, "replans": 1})
    assert drawn["chief"]["doing"] == "replanning"
    assert drawn["chief"]["replans"] == 1 and drawn["chief"]["max_replans"] == 2
    assert drawn["phase"] == "chief"


def test_review_and_the_answer_are_their_own_lanes() -> None:
    review = [event("step_started", 0, node="critique")]
    assert flow(events=review)["phase"] == "critic"
    answer = [event("step_started", 0, node="synthesis")]
    drawn = flow(events=answer)
    assert drawn["phase"] == "answer" and drawn["answer"]["state"] == "writing"


def test_the_progress_check_is_shown_as_a_rule_not_as_jev() -> None:
    events = [
        event("step_started", 0, node="research"),
        event("step_completed", 10, node="work"),
        event("step_started", 11, node="plan"),
        event("step_completed", 20, node="replan"),
        event("step_started", 21, node="research"),
        event("step_completed", 30, node="work"),
        event("step_started", 31, node="critique"),
    ]
    decisions = flow(events=events)["decisions"]
    assert [d["answer"] for d in decisions] == ["replan", "to review"]
    assert all(d["by"] == "rule" for d in decisions)


def test_jevs_judgments_appear_only_when_recorded_and_are_graded() -> None:
    assert flow()["jev"]["judgments"] == []
    state = {
        "plan": PLAN,
        "step_status": {},
        "convergence": {"method": "typesafe", "still_raised": 1, "total": 4},
        "source_checks": [
            {"url": "https://a", "declared": "primary", "judged": "secondary", "confidence": 0.9}
        ],
        "claim_check": "typesafe",
        "unverified_claims": ["one", "two"],
    }
    events = [event("related_work", 0, runs=["r1", "r2"])]
    line = {"name": "Fintech", "set_by": "office", "confidence": 0.53}
    judged = {
        j["label"]: j for j in flow(state=state, events=events, line=line)["jev"]["judgments"]
    }
    assert judged["Related work"]["answer"] == "2 earlier answers"
    assert judged["Business line"]["confidence"] == 0.53
    assert judged["Business line"]["grade"] == "split"
    assert judged["Source labels"]["grade"] == "sharp"
    assert judged["Same concerns again"]["confidence"] == 0.25
    assert judged["Claims checked"]["answer"] == "2 not supported by the research"


def test_a_line_the_owner_chose_is_not_a_judgment() -> None:
    line = {"name": "Fintech", "set_by": "owner", "confidence": None}
    labels = [j["label"] for j in flow(line=line)["jev"]["judgments"]]
    assert "Business line" not in labels


def test_the_models_are_the_ones_that_ran_each_role() -> None:
    usage = [
        {"role": "planner", "model": "gpt-5.6-sol", "calls": 3, "tokens": 900},
        {"role": "chief", "model": "gpt-5.6-sol", "calls": 1, "tokens": 400},
        {"role": "researcher", "model": "deepseek-flash", "calls": 8, "tokens": 5000},
        {"role": "summarist", "model": "deepseek-flash", "calls": 1, "tokens": 700},
        {"role": "critic", "model": "gpt-5.6-luna", "calls": 2, "tokens": 800},
    ]
    drawn = flow(usage=usage)
    assert drawn["chief"]["model"] == "gpt-5.6-sol" and drawn["chief"]["calls"] == 4
    assert drawn["critic"]["model"] == "gpt-5.6-luna"
    models = {card["id"]: card["model"] for card in drawn["steps"]}
    assert models["rules"] == "deepseek-flash" and models["report"] == "deepseek-flash"


def test_the_log_reads_as_what_happened_with_repeats_folded() -> None:
    events = [
        event("run_started", 0),
        event("step_started", 1, node="plan"),
        event("step_completed", 30, node="plan"),
        event("step_started", 31, node="research"),
        event("step_started", 31.01, node="research"),
        event("step_started", 31.02, node="research"),
    ]
    log = flow(events=events)["log"]
    assert [(row["actor"], row["text"]) for row in log] == [
        ("office", "started"),
        ("chief", "planning"),
        ("chief", "plan ready"),
        ("researcher", "3 steps started"),
    ]


def test_a_fixed_pipeline_task_shows_its_stages_as_the_helpers() -> None:
    events = [
        event("step_started", 0, node="plan"),
        event("step_completed", 10, node="plan"),
        event("step_started", 11, node="research"),
        event("step_completed", 40, node="research"),
        event("step_started", 41, node="strategy"),
    ]
    drawn = flow(state={"plan": {"steps": []}}, events=events)
    cards = {card["id"]: card["status"] for card in drawn["steps"]}
    assert cards == {"research": "done", "strategy": "running"}


def test_the_status_line_sums_up_the_task() -> None:
    state = {"plan": PLAN, "step_status": {"rules": "done"}, "replans": 1, "waves": 3}
    line = flow(state=state)["status_line"]
    assert line["efforts"] == {"standard": 3, "deep": 1}
    assert line["done"] == 1 and line["steps"] == 4
    assert line["replans"] == "1/2" and line["waves"] == 3


def test_the_advisor_lane_says_when_it_is_off() -> None:
    lane = flow()["advisor"]
    assert lane["on"] is False and lane["consulted"] == []


def test_the_advisor_lane_shows_each_moment_and_its_latest_word() -> None:
    advice = [
        {
            "moment": "before_plan",
            "verdict": "continue",
            "advice": [],
            "watch_for": ["dates"],
            "at": (START + timedelta(seconds=40)).isoformat(),
        },
        {
            "moment": "before_done",
            "verdict": "change_course",
            "advice": ["Cite the register"],
            "watch_for": [],
            "at": (START + timedelta(seconds=90)).isoformat(),
        },
    ]
    usage = [{"role": "advisor", "model": "claude-fable-5-1", "calls": 2, "tokens": 48000}]
    events = [event("step_started", 0, node="plan"), event("step_started", 100, node="synthesis")]
    drawn = flow(
        state={"plan": PLAN, "step_status": {}, "advice": advice},
        events=events,
        usage=usage,
        advisor_on=True,
    )
    lane = drawn["advisor"]
    assert lane["on"] is True and lane["model"] == "claude-fable-5-1"
    assert lane["calls"] == 2 and lane["tokens"] == 48000
    assert [m["moment"] for m in lane["moments"]] == [
        "before_plan",
        "repeated_failure",
        "before_done",
    ]
    assert [m["verdict"] for m in lane["moments"]] == ["continue", None, "change_course"]
    assert lane["latest"]["advice"] == ["Cite the register"]
    said = [(row["actor"], row["text"]) for row in drawn["log"]]
    assert ("advisor", "before the plan · continue") in said
    assert said.index(("advisor", "before the plan · continue")) == 1, "in time order"
    assert said[-1] == ("chief", "writing the answer")


def test_a_rejected_output_says_why_in_the_log() -> None:
    """The owner could not tell why a step failed twice in the middle."""
    rejections = [
        {
            "role": "researcher",
            "status": "rejected",
            "attempts": 3,
            "detail": "attempt 1: not valid JSON: it ends before the object is complete; "
            "attempt 2: key_findings: Field required",
            "at": START + timedelta(seconds=50),
        },
        {
            "role": "planner",
            "status": "ok",
            "attempts": 2,
            "detail": "attempt 1: steps.0.kind: Input should be 'research' or 'write'",
            "at": START + timedelta(seconds=70),
        },
    ]
    events = [event("step_started", 0, node="research"), event("step_completed", 60, node="work")]
    said = [
        (row["actor"], row["text"]) for row in flow(events=events, rejections=rejections)["log"]
    ]
    assert said[1] == (
        "researcher",
        "output rejected after 3 tries · it ends before the object is complete",
    )
    assert said[3] == ("chief", "output corrected on try 2")


def test_the_flow_carries_the_tasks_room() -> None:
    room = [
        {
            "kind": "claim",
            "author_role": "researcher",
            "step_id": "a",
            "text": "Taking: rules",
            "at": "x",
        },
        {
            "kind": "challenge",
            "author_role": "critic",
            "step_id": None,
            "to_step": "b",
            "text": "Primary source?",
            "at": "y",
        },
    ]
    drawn = flow(room=room, team_on=True)
    assert drawn["room"]["posts"] == room and drawn["room"]["count"] == 2
    assert drawn["room"]["team"] is True
    assert [s["label"] for s in drawn["room"]["sections"]] == ["Wave 1", "Huddle 1 · after wave 1"]
    assert flow()["room"] == {
        "posts": [],
        "sections": [],
        "talk": [],
        "count": 0,
        "team": False,
        "huddles": 0,
    }
