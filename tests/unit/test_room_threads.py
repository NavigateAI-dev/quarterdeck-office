"""The task's room as a conversation: grouped by stage, replies under what they answer."""

from typing import Any

from app.team_room import threaded


def post(key: str, kind: str, author: str, text: str = "", **rest: Any) -> dict[str, Any]:
    return {"key": key, "kind": kind, "author_role": author, "text": text or key, **rest}


def labels(sections: list[dict[str, Any]]) -> list[str]:
    return [s["label"] for s in sections]


def keys(section: dict[str, Any]) -> list[str]:
    return [p["key"] for p in section["posts"]]


def test_posts_are_grouped_by_the_stage_they_were_said_in() -> None:
    sections = threaded(
        [
            post("p", "plan", "chief", phase="plan"),
            post("c1", "claim", "researcher", step_id="a", phase="work", wave=1),
            post("f1", "finding", "researcher", step_id="a", phase="work", wave=1),
            post("h1", "need", "writer", phase="huddle", wave=1),
            post("c2", "claim", "researcher", step_id="b", phase="work", wave=2),
            post("h2", "challenge", "critic", phase="huddle", wave=2),
            post("r", "challenge", "critic", to_role="writer", phase="review", wave=2),
            post("done", "note", "chief", phase="answer"),
        ]
    )
    assert labels(sections) == [
        "Planning",
        "Wave 1",
        "Huddle 1 · after wave 1",
        "Wave 2",
        "Huddle 2 · after wave 2",
        "Review",
        "Answer",
    ]
    assert keys(sections[1]) == ["c1", "f1"]


def test_a_reply_sits_under_the_post_it_answers_even_from_a_later_stage() -> None:
    sections = threaded(
        [
            post("offer", "offer", "advisor", phase="huddle", wave=1),
            post("added", "plan", "chief", phase="huddle", wave=1, reply_to="offer"),
            post("claim", "claim", "researcher", step_id="offer-1", phase="work", wave=2),
            post("rerun", "claim", "researcher", step_id="b", phase="work", wave=2, reply_to="c"),
        ]
    )
    huddle = sections[0]
    assert keys(huddle) == ["offer"]
    assert [r["key"] for r in huddle["posts"][0]["replies"]] == ["added"]
    assert huddle["count"] == 2
    # A reply to a post not shown stands on its own.
    assert keys(sections[1]) == ["claim", "rerun"]


def test_a_post_to_a_step_answers_that_steps_latest_word() -> None:
    sections = threaded(
        [
            post("f", "doubt", "researcher", step_id="autogen", phase="work", wave=1),
            post("n", "need", "writer", to_step="autogen", phase="huddle", wave=1),
            post("ahead", "need", "writer", to_step="report", phase="huddle", wave=1),
        ]
    )
    assert [r["key"] for r in sections[0]["posts"][0]["replies"]] == ["n"]
    assert keys(sections[1]) == ["ahead"], "nothing yet from the report step to answer"


def test_the_owners_post_joins_the_stage_it_arrived_in() -> None:
    sections = threaded(
        [
            post("c1", "claim", "researcher", step_id="a", phase="work", wave=1),
            post("me", "owner", "owner", to_step="report"),
        ]
    )
    assert labels(sections) == ["Wave 1"]
    assert keys(sections[0]) == ["c1", "me"]


def test_an_older_room_is_read_into_stages_from_its_order() -> None:
    # Posts written before the room kept stages: the T4 trial's shape.
    sections = threaded(
        [
            post("adv", "advice", "advisor", "Continue."),
            post("plan", "plan", "chief", "Plan: 3 steps."),
            post("c1", "claim", "researcher", step_id="a"),
            post("d1", "doubt", "researcher", step_id="a"),
            post("n1", "need", "writer", to_step="a"),
            post("o1", "offer", "advisor", "Add a recovery pass for AutoGen docs"),
            post(
                "add",
                "plan",
                "chief",
                "Added offer-1 at the advisor's offer: Add a recovery pass for AutoGen docs",
            ),
            post("c2", "claim", "researcher", step_id="offer-1"),
            post("x", "challenge", "critic", "b cites a blog", to_step="b"),
            post("rr", "note", "chief", "Rerunning b to answer: b cites a blog"),
            post("rev", "challenge", "critic", "No report.", to_role="writer"),
            post("ok", "note", "chief", "The answer is written."),
        ]
    )
    assert labels(sections) == [
        "Planning",
        "Wave 1",
        "Huddle 1 · after wave 1",
        "Wave 2",
        "Huddle 2 · after wave 2",
        "Review",
        "Answer",
    ]
    wave1 = sections[1]
    assert [r["key"] for r in wave1["posts"][1]["replies"]] == ["n1"]
    huddle1 = sections[2]
    assert keys(huddle1) == ["o1"]
    assert [r["key"] for r in huddle1["posts"][0]["replies"]] == ["add"]
    assert [r["key"] for r in sections[4]["posts"][0]["replies"]] == ["rr"]


def test_an_empty_room_has_no_stages() -> None:
    assert threaded([]) == []


# A huddle in one line, for the project room.


def test_a_huddle_is_said_in_one_line_from_its_posts() -> None:
    from app.team_room import huddle_summary

    posts = [
        post("c1", "claim", "researcher", step_id="a", phase="work", wave=2),
        post("x", "challenge", "critic", phase="huddle", wave=2),
        post("y", "challenge", "critic", phase="huddle", wave=2),
        post("n", "need", "writer", phase="huddle", wave=2),
        post("o", "offer", "advisor", "Add an AutoGen pass", phase="huddle", wave=2),
        post(
            "a",
            "plan",
            "chief",
            "Added offer-1 at the advisor's offer: Add an AutoGen pass",
            phase="huddle",
            wave=2,
            reply_to="o",
        ),
        post("z", "challenge", "critic", phase="huddle", wave=3),
    ]
    line = huddle_summary(posts, 2)
    assert line == (
        "Huddle after wave 2 — Critic: 2 challenges · Writer: 1 request · Advisor: 1 offer. "
        "Chief added offer-1. First challenge (Critic): “x”"
    )


def test_a_quiet_huddle_says_so() -> None:
    from app.team_room import huddle_summary

    assert huddle_summary([], 1) == "Huddle after wave 1: nothing raised."


def test_an_older_rooms_huddle_is_said_from_its_order() -> None:
    from app.team_room import huddle_summary

    posts = [
        post("c1", "claim", "researcher", step_id="a"),
        post("n", "need", "writer", to_step="a"),
        post("c2", "claim", "researcher", step_id="b"),
    ]
    assert huddle_summary(posts, 1) == "Huddle after wave 1 — Writer: 1 request."
