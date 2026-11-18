"""T: agents as a team in one room — the board every agent writes and reads."""

from dataclasses import replace
from typing import Any

from app.orchestrator.graph import run_analysis
from app.orchestrator.pipeline import build_graph
from app.schemas.agent_output import Advice
from tests.unit.test_advisor import CONTINUE
from tests.unit.test_graph import Role, agents, critique, start
from tests.unit.test_step_executor import EMPTY, planned, step


class Board:
    """What the dispatcher keeps on the run's record, in memory."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def post(self, entry: dict[str, Any]) -> None:
        if any(p["key"] == entry["key"] for p in self.posts):
            return
        self.posts.append(entry)

    def read(self) -> list[dict[str, Any]]:
        return list(self.posts)


async def run(
    plans: list[dict[str, Any]],
    critiques: list[dict[str, Any]],
    research: list[dict[str, Any]] | None = None,
    advisor: Any = None,
    board: Board | None = None,
) -> tuple[Any, dict[str, Any], Board]:
    board = board or Board()
    agent_set, roles = agents(critiques)
    roles["planner"].outputs = plans
    if research is not None:
        roles["researcher"].outputs = research
    if advisor is not None:
        agent_set = replace(agent_set, advisor=advisor)
    state = await run_analysis(build_graph(agent_set, 3, board=board), start())
    return state, roles, board


def kinds(board: Board) -> list[tuple[str, str, str | None]]:
    return [(p["kind"], p["author_role"], p.get("step_id")) for p in board.posts]


# The work posts itself, with no extra model call.


async def test_the_work_posts_itself_to_the_room() -> None:
    _, roles, board = await run(
        [planned(step("a"), step("b"), step("w", "write", ["a", "b"]))], [critique("PASS")]
    )
    seen = kinds(board)
    assert seen[0] == ("plan", "chief", None)
    assert ("claim", "researcher", "a") in seen and ("claim", "researcher", "b") in seen
    assert ("finding", "researcher", "a") in seen and ("finding", "researcher", "b") in seen
    assert ("claim", "writer", "w") in seen and ("draft", "writer", "w") in seen
    assert seen.index(("claim", "researcher", "a")) < seen.index(("finding", "researcher", "a"))
    assert seen.index(("finding", "researcher", "a")) < seen.index(("claim", "writer", "w"))
    assert seen[-1][0] == "note" and seen[-1][1] == "chief", "the answer is announced"
    # Nothing here cost a model call the relay would not have made.
    assert len(roles["researcher"].calls) == 2 and len(roles["critic"].calls) == 1


async def test_a_step_that_finds_nothing_says_so() -> None:
    _, _, board = await run(
        [planned(step("a")), planned(step("b"))], [critique("PASS")], research=[EMPTY]
    )
    doubts = [p for p in board.posts if p["kind"] == "doubt"]
    assert doubts and doubts[0]["step_id"] == "a"
    assert any(p["kind"] == "plan" and "Replanned" in p["text"] for p in board.posts)


async def test_the_critic_posts_its_challenges_to_whom_they_concern() -> None:
    revise = critique("REVISE", "research") | {
        "critical_issues": ["The BLS figures were never extracted", "No positive case"]
    }
    _, _, board = await run([planned(step("a")), planned(step("a2"))], [revise, critique("PASS")])
    challenges = [p for p in board.posts if p["kind"] == "challenge"]
    assert [c["text"] for c in challenges][:2] == [
        "The BLS figures were never extracted",
        "No positive case",
    ]
    assert all(c["author_role"] == "critic" and c["to_role"] == "researcher" for c in challenges)


async def test_the_advisor_speaks_in_the_room_too() -> None:
    advisor = Role("advisor", Advice, [CONTINUE])
    _, _, board = await run([planned(step("a"))], [critique("PASS")], advisor=advisor)
    assert any(p["kind"] == "advice" and p["author_role"] == "advisor" for p in board.posts)


async def test_a_post_said_twice_is_kept_once() -> None:
    """A step run again after a pause must not fill the room with repeats."""
    board = Board()
    await run([planned(step("a"))], [critique("PASS")], board=board)
    before = len(board.posts)
    await run([planned(step("a"))], [critique("PASS")], board=board)
    assert len(board.posts) == before


async def test_without_a_board_nothing_changes() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [planned(step("a"))]
    state = await run_analysis(build_graph(agent_set, 3), start())
    assert state["status"] == "COMPLETED"


# Seeing peers while working, and huddles after each wave.

from app.schemas import ResearchReport  # noqa: E402
from app.schemas.agent_output import Reaction  # noqa: E402
from tests.unit.test_graph import RESEARCH  # noqa: E402


class Peer(Role):
    """A researcher that reads the room after its own reading, as a real one does."""

    def __init__(self) -> None:
        super().__init__("researcher", ResearchReport, [RESEARCH])
        self.read: list[str] = []

    async def run(self, objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        late = getattr(self, "late_inputs", None)
        self.read.append(late() if callable(late) else "")
        return await super().run(objective, context, inputs, focus)


def huddle_with(answers: dict[str, list[dict[str, Any]]]) -> tuple[Any, dict[str, list[str]]]:
    """Fake teammates: each role answers with its queued posts."""
    asked: dict[str, list[str]] = {}

    def teammate(agent: Any, role: str) -> Any:
        fake = Role(role, Reaction, [{"posts": answers.get(role, [])}])
        original = fake.run

        async def run(objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
            asked.setdefault(role, []).append(inputs)
            return await original(objective, context, inputs, focus)

        fake.run = run  # type: ignore[method-assign]
        return fake

    return teammate, asked


async def team_run(
    plans: list[dict[str, Any]],
    answers: dict[str, list[dict[str, Any]]],
    team: bool = True,
) -> tuple[Any, dict[str, Any], Board, Peer, dict[str, list[str]]]:
    board = Board()
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = plans
    peer = Peer()
    agent_set = replace(agent_set, researcher=peer)
    roles["researcher"] = peer
    teammate, asked = huddle_with(answers)
    graph = build_graph(agent_set, 3, board=board, team=team, teammate=teammate)
    state = await run_analysis(graph, start())
    return state, roles, board, peer, asked


TWO_WAVES = planned(
    step("a"), step("b"), step("c", "research", ["a"]), step("w", "write", ["c", "b"])
)


async def test_a_step_reads_what_its_peers_posted_and_not_its_own() -> None:
    _, _, board, peer, _ = await team_run([TWO_WAVES], {})
    # The step that runs after the first wave sees that wave's findings.
    later = peer.read[-1]
    assert "Your teammates" in later
    assert "a:" in later or "[researcher · a]" in later
    assert all("Taking: do c" not in read for read in peer.read), (
        "a step is not shown its own claim"
    )


async def test_with_team_mode_off_no_one_reads_the_room_and_no_one_huddles() -> None:
    _, _, board, peer, asked = await team_run([TWO_WAVES], {}, team=False)
    assert all(read == "" for read in peer.read)
    assert asked == {}
    assert any(p["kind"] == "finding" for p in board.posts), "the room is still written"


async def test_a_huddle_after_a_wave_posts_each_roles_reaction() -> None:
    answers = {
        "critic": [
            {
                "kind": "challenge",
                "to_step": "c",
                "text": "Step a cites a vendor blog; read the regulation.",
            }
        ],
        "writer": [
            {"kind": "need", "to_step": None, "text": "The report will need prices by region."}
        ],
    }
    state, roles, board, peer, asked = await team_run([TWO_WAVES], answers)
    assert set(asked) == {"critic", "writer"}, "the critic and the writer react"
    huddled = [p for p in board.posts if p["kind"] in ("challenge", "need")]
    assert {(p["author_role"], p["kind"]) for p in huddled} >= {
        ("critic", "challenge"),
        ("writer", "need"),
    }
    assert any(p.get("to_step") == "c" for p in huddled)
    # What the huddle said reaches the steps that come after it.
    assert "read the regulation" in peer.read[-1]
    # And the critic's final review is told what was raised in the room.
    assert "read the regulation" in roles["critic"].inputs[-1]
    assert state["status"] == "COMPLETED"


async def test_no_huddle_after_a_wave_that_found_nothing() -> None:
    """Live, 3 Oct: after each wave whose sources failed, the critic, advisor and
    writer asked for more verification, and the task grew steps to match."""
    board = Board()
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [TWO_WAVES]
    roles["researcher"].outputs = [EMPTY]
    teammate, asked = huddle_with(
        {"critic": [{"kind": "challenge", "to_step": "c", "text": "Verify more."}]}
    )
    graph = build_graph(agent_set, 3, board=board, team=True, teammate=teammate)
    await run_analysis(graph, start())
    assert asked == {}, "nothing was found, so there is nothing to react to"


async def test_no_huddle_when_nothing_is_left_to_shape() -> None:
    _, _, _, _, asked = await team_run(
        [planned(step("a"))], {"critic": [{"kind": "note", "to_step": None, "text": "x"}]}
    )
    assert asked == {}, "a single wave ends in review, which the critic already does"


async def test_a_huddle_that_fails_is_skipped() -> None:
    board = Board()
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [TWO_WAVES]

    def teammate(agent: Any, role: str) -> Any:
        from tests.unit.test_advisor import Down

        return Down("invalid_output")

    state = await run_analysis(
        build_graph(agent_set, 3, board=board, team=True, teammate=teammate), start()
    )
    assert state["status"] == "COMPLETED"


# Offers become steps, and challenges are resolved.

THREE_AND_MORE = planned(
    step("a"),
    step("b"),
    step("d"),
    step("c", "research", ["a"]),
    step("w", "write", ["c", "b", "d"]),
)


def briefs(peer: Peer) -> list[str]:
    return list(peer.calls)


async def test_a_challenge_to_a_finished_step_reruns_it_with_the_challenge_in_hand() -> None:
    answers = {
        "critic": [
            {"kind": "challenge", "to_step": "b", "text": "b cites a blog; read the regulation."}
        ]
    }
    state, _, board, peer, _ = await team_run([TWO_WAVES], answers)
    assert briefs(peer).count("do b") == 2, "b ran again"
    reran = [i for i, brief in enumerate(briefs(peer)) if brief == "do b"][1]
    assert "read the regulation" in peer.read[reran], "and read the challenge before writing"
    assert any(p["kind"] == "note" and "Rerunning b" in p["text"] for p in board.posts)
    settled = {c["to_step"]: c["outcome"] for c in state["challenges"]}
    assert settled == {"b": "answered"}


async def test_reruns_are_bounded_and_the_rest_are_stated_limits() -> None:
    answers = {
        "critic": [
            {"kind": "challenge", "to_step": "a", "text": "a: wrong year."},
            {"kind": "challenge", "to_step": "b", "text": "b: secondary source."},
            {"kind": "challenge", "to_step": "d", "text": "d: no figures."},
        ]
    }
    state, roles, _, peer, _ = await team_run([THREE_AND_MORE], answers)
    outcomes = sorted(c["outcome"] for c in state["challenges"])
    assert outcomes == ["answered", "answered", "limit"]
    limited = next(c["text"] for c in state["challenges"] if c["outcome"] == "limit")
    assert (
        "could not resolve" in roles["critic"].inputs[-1] and limited in roles["critic"].inputs[-1]
    )
    assert limited in roles["chief"].inputs[-1], "the answer states it as a limit"


async def test_an_offer_is_noted_for_the_chief_not_added_as_a_step() -> None:
    """Q2, 8 Oct: offers taken as steps grew plans into audits nobody asked for."""
    answers = {
        "writer": [
            {"kind": "offer", "to_step": None, "text": "Check Google Play's transfer rules."}
        ]
    }
    state, roles, board, peer, _ = await team_run([TWO_WAVES], answers)
    assert "Check Google Play's transfer rules." not in briefs(peer)
    assert not [s for s in state["plan"]["steps"] if s["id"].startswith("offer-")]
    assert any(p["kind"] == "note" and "Noted the writer's offer" in p["text"] for p in board.posts)


# The owner in the room.


async def test_the_owners_post_to_a_finished_step_reruns_it() -> None:
    board = Board()
    board.post(
        {
            "key": "owner-1",
            "kind": "owner",
            "author_role": "owner",
            "step_id": None,
            "to_role": None,
            "to_step": "a",
            "text": "Use the 2026 figures, not 2024.",
        }
    )
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [TWO_WAVES]
    peer = Peer()
    agent_set = replace(agent_set, researcher=peer)
    teammate, _ = huddle_with({})
    state = await run_analysis(
        build_graph(agent_set, 3, board=board, team=True, teammate=teammate), start()
    )
    assert peer.calls.count("do a") == 2
    assert any("2026 figures" in read for read in peer.read)
    assert {c["to_step"]: c["outcome"] for c in state["challenges"]} == {"a": "answered"}


# The room as a conversation: each post knows its stage and what it answers.


def said(board: Board, kind: str, needle: str = "") -> dict[str, Any]:
    return next(p for p in board.posts if p["kind"] == kind and needle in p["text"])


async def test_each_post_carries_the_stage_it_was_said_in() -> None:
    answers = {"writer": [{"kind": "need", "to_step": "c", "text": "Prices by region."}]}
    _, _, board, _, _ = await team_run([TWO_WAVES], answers)
    assert said(board, "plan", "Plan:")["phase"] == "plan"
    first = said(board, "claim", "do a")
    assert (first["phase"], first["wave"]) == ("work", 1)
    later = said(board, "claim", "do c")
    assert (later["phase"], later["wave"]) == ("work", 2)
    need = said(board, "need")
    assert (need["phase"], need["wave"]) == ("huddle", 1)
    assert said(board, "note", "answer is written")["phase"] == "answer"


async def test_the_chief_acting_on_a_post_replies_to_it() -> None:
    answers = {
        "writer": [{"kind": "offer", "to_step": None, "text": "Check the transfer rules."}],
        "critic": [{"kind": "challenge", "to_step": "b", "text": "b cites a blog."}],
    }
    _, _, board, _, _ = await team_run([TWO_WAVES], answers)
    offer, challenge = said(board, "offer"), said(board, "challenge", "b cites")
    assert said(board, "note", "Noted the writer's offer")["reply_to"] == offer["key"]
    assert said(board, "note", "Rerunning b")["reply_to"] == challenge["key"]
    rerun = [p for p in board.posts if p["kind"] == "claim" and p.get("step_id") == "b"][-1]
    assert rerun["reply_to"] == challenge["key"], "the step run again answers the challenge"


async def test_a_teammate_answers_a_post_by_its_number() -> None:
    answers = {"critic": [{"kind": "challenge", "to_step": None, "text": "Wrong.", "reply_to": 2}]}
    _, _, board, _, asked = await team_run([TWO_WAVES], answers)
    assert "#2 " in asked["critic"][0], "the room is shown numbered"
    assert said(board, "challenge", "Wrong.")["reply_to"] == board.posts[1]["key"]


async def test_a_number_that_names_no_post_is_ignored() -> None:
    answers = {"critic": [{"kind": "note", "to_step": None, "text": "Hm.", "reply_to": 99}]}
    _, _, board, _, _ = await team_run([TWO_WAVES], answers)
    assert said(board, "note", "Hm.").get("reply_to") is None


async def test_each_huddle_is_marked_once_for_the_project_room() -> None:
    marked: list[int] = []

    class Marking(Board):
        def huddled(self, wave: int) -> None:
            marked.append(wave)

    board = Marking()
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [TWO_WAVES]
    teammate, _ = huddle_with({"writer": [{"kind": "need", "to_step": None, "text": "Prices."}]})
    await run_analysis(
        build_graph(agent_set, 3, board=board, team=True, teammate=teammate), start()
    )
    assert marked == [1, 2], "one mark per huddle: after waves 1 and 2, not after the last"
