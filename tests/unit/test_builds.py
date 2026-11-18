"""Building something: the plan, the proposal, the project folder."""

from pathlib import Path
from typing import Any

import pytest

from app import builds
from app.approvals import describe_consequence
from app.orchestrator.engineering import DisabledEngineering
from app.orchestrator.graph import run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.state import RunStatus
from tests.unit.test_approval_graph import Gate, scope
from tests.unit.test_graph import PLAN, SYNTHESIS, agents, critique, start

SPEC = {
    "summary": "A colouring-page website for children.",
    "features": ["Browse pages by theme", "Preview a page", "Print it on A4"],
    "stack": "Plain HTML, CSS and JavaScript, no build step",
    "how_to_run": "Open index.html",
    "checks": ["index.html exists", "the JavaScript parses"],
}
BUILD_PLAN = PLAN | {"task_kind": "build", "build_spec": SPEC, "steps": []}


class Seen(Gate):
    def __init__(self, verdict: str | None) -> None:
        super().__init__(verdict)
        self.proposals: list[dict[str, Any]] = []

    def open(self, objective: str, synthesis: dict[str, Any]) -> tuple[str, str]:
        self.proposals.append(synthesis)
        return super().open(objective, synthesis)


async def test_a_build_goes_from_its_plan_straight_to_a_proposal() -> None:
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [BUILD_PLAN]
    # A Chief that leaves the scope out: the plan's spec is what is proposed.
    roles["chief"].outputs = [
        SYNTHESIS | {"proposed_engineering_scope": "", "proposes_action": False}
    ]
    gate = Seen(None)
    graph = build_graph(agent_set, 3, gate=gate, executor=DisabledEngineering())
    state = await run_analysis(graph, start())
    assert state["status"] == RunStatus.WAITING_FOR_APPROVAL
    assert roles["researcher"].calls == [] and roles["critic"].calls == []
    proposal = gate.proposals[0]
    assert proposal["building"] is True and proposal["proposes_action"] is True
    assert "Print it on A4" in proposal["proposed_engineering_scope"]
    assert proposal["engineering_acceptance_criteria"] == SPEC["checks"]
    assert "This task builds something" in roles["chief"].inputs[-1]


def test_a_build_plan_must_say_what_it_builds() -> None:
    from pydantic import ValidationError

    from app.schemas import TaskPlan

    with pytest.raises(ValidationError):
        TaskPlan.model_validate(PLAN | {"task_kind": "build"})


def test_starting_a_build_says_what_happens_in_plain_words() -> None:
    said = describe_consequence(
        scope(lands_in_source=True, executes_code=True, coding_agent="codex")
    )
    assert "Codex writes it" in said and "Nothing is published" in said
    off = describe_consequence(scope(lands_in_source=True, executes_code=False))
    assert "nothing would be built" in off


def test_new_fields_keep_old_approvals_digest_as_it_was() -> None:
    before = scope()
    same = before.model_copy(update={"lands_in_source": False, "coding_agent": ""})
    assert before.digest() == same.digest()
    assert before.digest() != before.model_copy(update={"lands_in_source": True}).digest()


def test_a_project_folder_is_a_repository_with_a_first_commit(tmp_path: Path) -> None:
    folder = tmp_path / "builds" / "abc"
    builds.ensure_repository(folder)
    builds.ensure_repository(folder)  # Made once; a second call changes nothing.
    assert (folder / ".git").is_dir()
    assert [row["subject"] for row in builds.history(folder)] == ["Start"]


def test_files_are_read_only_from_inside_the_folder(tmp_path: Path) -> None:
    folder = tmp_path / "site"
    (folder / "css").mkdir(parents=True)
    (folder / "index.html").write_text("<h1>Hi</h1>")
    (folder / "css" / "site.css").write_text("h1{}")
    (tmp_path / "secret.txt").write_text("no")
    (folder / "escape").symlink_to(tmp_path / "secret.txt")
    listed = [row["path"] for row in builds.files(folder)]
    assert listed == ["css/site.css", "index.html"]
    assert builds.read_file(folder, "index.html")[0] == b"<h1>Hi</h1>"
    for bad in ("../secret.txt", "/etc/passwd", "escape", ".git/config"):
        with pytest.raises(builds.BuildError):
            builds.read_file(folder, bad)


def test_a_build_downloads_as_one_folder_in_a_zip(tmp_path: Path) -> None:
    import io
    import zipfile

    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>Hi</h1>")
    names = zipfile.ZipFile(io.BytesIO(builds.zipped(folder, "colouring"))).namelist()
    assert names == ["colouring/index.html"]


async def test_after_a_build_its_answer_says_how_it_ended() -> None:
    """D1: the answer used to say "press Start building" after the build had run."""
    agent_set, roles = agents([critique("PASS")])
    roles["planner"].outputs = [BUILD_PLAN]
    gate = Seen("APPROVED")
    graph = build_graph(agent_set, 3, gate=gate, executor=DisabledEngineering())
    state = await run_analysis(graph, start())
    assert state["status"] == RunStatus.FAILED
    assert state["synthesis"]["lead"] == "Nothing was built: code execution is off for this office."
    assert "Try the build again" in state["synthesis"]["next_steps"][0]


def test_a_finished_build_leads_with_what_was_made() -> None:
    from app.orchestrator.graph import build_outcome

    told = build_outcome(
        {"synthesis": {"lead": "Ready to build", "facts": ["x"], "recommendation": "r"}},  # type: ignore[arg-type]
        {"status": "completed", "summary": "A colouring site with 6 pictures.\nMore."},
    )
    lead = told["synthesis"]["lead"]
    assert lead.startswith("Built and checked. A colouring site with 6 pictures.")
    assert "Ready to build" not in lead


def test_a_failed_build_says_why_in_the_tasks_language_and_drops_stale_advice() -> None:
    """Fix 1 (task 3c555109): an English line on a Chinese plan, still saying Start building."""
    from app.orchestrator.graph import build_outcome

    before = {
        "objective": "制作儿童填色图网站 V2，按年龄分组",
        "synthesis": {
            "lead": "建议制作 V2。",
            "facts": ["x"],
            "recommendation": "按“Start building”制作 V2。",
        },
    }
    told = build_outcome(
        before,  # type: ignore[arg-type]
        {
            "status": "failed",
            "errors": ["tests_failed"],
            "summary": "无法完成可靠交付：执行环境发生基础设施故障，返回 ENOENT。",
            "changed_files": ["index.html", "app.js", "server.js"],
        },
    )["synthesis"]
    assert told["lead"].startswith("编码代理在工作时其沙箱发生故障")
    assert "写了 3 个文件" in told["lead"] and "仍是之前的版本" in told["lead"]
    assert "基础设施故障" in told["lead"], "the agent's own words are kept"
    assert "Start building”制作" not in told["recommendation"]
    assert "Try the build again" in told["recommendation"]


def test_a_failed_build_in_english_says_what_was_written() -> None:
    from app.orchestrator.graph import build_outcome

    told = build_outcome(
        {"objective": "Build version 2", "synthesis": {"lead": "Ready", "recommendation": "Build"}},  # type: ignore[arg-type]
        {
            "status": "failed",
            "errors": ["tests_failed"],
            "summary": "Ran out of ideas.",
            "changed_files": ["a", "b"],
        },
    )["synthesis"]
    assert told["lead"].startswith("The build did not pass its own checks, even after a round")
    assert "wrote 2 files, but none were kept" in told["lead"]
    assert told["recommendation"].startswith("Press Try the build again")
