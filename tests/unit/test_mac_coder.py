"""Quarterdeck Coder, the helper on the Mac (Stage 36a), against real git folders
and a stand-in CLI."""

import io
import json
import subprocess
import tarfile
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from scripts import mac_coder
from scripts.mac_coder import Job, Refused, command_for, prepare, take_over

# Prints Claude Code's stream, writes a file, says what it did; a second turn
# records the arguments it was given.
FAKE_CLI = """#!/bin/sh
echo "$@" | tr '\n' ' ' >> "$ARGS_LOG"; echo >> "$ARGS_LOG"
echo '{"type":"system","subtype":"init","session_id":"ignored"}'
echo 'hello' > page.txt
echo '{"type":"result","result":"Added page.txt"}'
"""
# DeepSeek Harness's headless stream: its own session id, a file, its answer.
FAKE_DSH = """#!/bin/sh
echo "$@" | tr '\n' ' ' >> "$ARGS_LOG"; echo >> "$ARGS_LOG"
echo '{"type":"session","sessionId":"session-abc","cwd":"."}'
echo 'hello' > page.txt
echo '{"type":"final","sessionId":"session-abc","reason":{"kind":"completed"},\
"text":"Added page.txt"}'
"""
SLOW_CLI = """#!/bin/sh
echo '{"type":"system","subtype":"init"}'
sleep 30
echo late > late.txt
"""


def git(*args: str, where: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed git commands
        ["git", *args],  # noqa: S607
        cwd=where,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dev = tmp_path / "dev"
    site = dev / "site"
    site.mkdir(parents=True)
    git("init", "-q", "-b", "main", where=site)
    (site / "README.md").write_text("site\n")
    git("add", "-A", where=site)
    git(*mac_coder.IDENTITY, "commit", "-q", "-m", "first", where=site)
    monkeypatch.setattr(mac_coder, "HOME", tmp_path / "helper")
    monkeypatch.setattr(mac_coder, "REPORT_EVERY", 0.05)
    monkeypatch.setenv("ARGS_LOG", str(tmp_path / "args.log"))
    return dev


def cli(tmp_path: Path, body: str = FAKE_CLI) -> str:
    # One file per script: sh reads a script as it runs, so it is never rewritten.
    path = tmp_path / f"fake-cli-{abs(hash(body))}"
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def job(**given: Any) -> dict[str, Any]:
    return {
        "id": "j1",
        "kind": "code",
        "folder": "site",
        "base_branch": "main",
        "base_commit": None,
        "branch": "office/add-page",
        "cli": "claude_code",
        "brief": "Add a page",
        "session": "",
    } | given


class Office:
    """The office as the helper sees it: records reports, answers as told."""

    def __init__(self, answers: list[dict[str, Any]] | None = None, seed: bytes = b"") -> None:
        self.calls: list[tuple[str, Any]] = []
        self.answers = answers or []
        self.seed = seed
        self.files: dict[str, bytes] = {}

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((path, body))
        return self.answers.pop(0) if self.answers else {"stop": False, "notes": []}

    def raw(self, method: str, path: str, body: Path | None = None) -> bytes:
        if method == "GET":
            return self.seed
        assert body is not None
        self.files[path.rsplit("/", 1)[-1]] = body.read_bytes()
        return b""

    def names(self, which: str) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(self.files[which])) as tar:
            return sorted(tar.getnames())

    def lines(self) -> list[str]:
        return [
            line for path, body in self.calls if path.endswith("/lines") for line in body["lines"]
        ]


# ---- the command lines ----


@pytest.mark.parametrize("cli_key", ["claude_code", "codex", "opencode", "dsh"])
@pytest.mark.parametrize("resumed", [False, True])
def test_no_cli_is_ever_told_to_skip_its_sandbox(cli_key: str, resumed: bool) -> None:
    command = command_for(cli_key, "/bin/x", "do it", "s-1", resumed, Path("/w"))
    joined = " ".join(command)
    assert "dangerously" not in joined and "bypass" not in joined
    assert "skip-permissions" not in joined and "danger-full-access" not in joined
    assert command[-1] == "do it" or "do it" in command


def test_claude_code_is_given_its_session_and_resumed_on_it() -> None:
    first = command_for("claude_code", "claude", "p", "s-1", False, Path("/w"))
    later = command_for("claude_code", "claude", "p", "s-1", True, Path("/w"))
    assert first[-2:] == ["--session-id", "s-1"] and later[-2:] == ["--resume", "s-1"]
    assert "acceptEdits" in first
    # Its own sandbox, on: it may run its checks, and only inside the sandbox.
    given = json.loads(first[first.index("--settings") + 1])
    assert given == {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}}
    assert "--settings" in later


def test_codex_writes_only_in_its_workspace_even_when_resumed() -> None:
    first = command_for("codex", "codex", "p", "", False, Path("/w"))
    later = command_for("codex", "codex", "p", "t-1", True, Path("/w"))
    assert first[first.index("--sandbox") + 1] == "workspace-write"
    assert 'sandbox_mode="workspace-write"' in later and "t-1" in later


def test_deepseek_harness_writes_only_in_its_worktree_and_asks_no_one() -> None:
    first = command_for("dsh", "dsh", "p", "", False, Path("/w"))
    later = command_for("dsh", "dsh", "p", "session-abc", True, Path("/w"))
    assert first[1:3] == ["--profile", "headless"] and "--json" in first
    assert first[first.index("--permission-mode") + 1] == "workspace-write"
    # Nobody is there to answer: a call that needs approval is refused, not run.
    assert first[first.index("--approval-policy") + 1] == "never"
    assert "--add-dir" not in first and "--session-id" not in first
    assert later[later.index("--session-id") + 1] == "session-abc"
    assert later[later.index("--permission-mode") + 1] == "workspace-write"


def test_a_cli_it_does_not_run_is_refused() -> None:
    with pytest.raises(Refused):
        command_for("aider", "aider", "p", "", False, Path("/w"))


def test_deepseek_harness_is_found_inside_its_app() -> None:
    assert mac_coder.DSH_IN_APP in mac_coder.CANDIDATES["dsh"]
    assert "dsh" in mac_coder.RUNS


def test_the_take_over_command_is_quoted() -> None:
    said = take_over("claude_code", Path("/Users/me/dev/my site"), "s-1")
    assert said == "cd '/Users/me/dev/my site' && claude --resume s-1"
    dsh = take_over("dsh", Path("/w"), "session-abc", "/Applications/A B.app/dsh")
    assert dsh == "cd /w && '/Applications/A B.app/dsh' tui --resume session-abc"


def test_sessions_and_last_words_are_read_from_each_cli() -> None:
    assert (
        mac_coder.session_in("codex", json.dumps({"type": "thread.started", "thread_id": "t"}))
        == "t"
    )
    assert mac_coder.session_in("opencode", json.dumps({"part": {"sessionID": "o"}})) == "o"
    assert (
        mac_coder.message_in("claude_code", json.dumps({"type": "result", "result": "ok"})) == "ok"
    )
    codex_said = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
    assert mac_coder.message_in("codex", json.dumps(codex_said)) == "done"
    assert mac_coder.message_in("codex", "not json") == ""
    started = {"type": "session", "sessionId": "session-abc", "cwd": "/w"}
    assert mac_coder.session_in("dsh", json.dumps(started)) == "session-abc"
    done = {"type": "final", "reason": {"kind": "completed"}, "text": "All done"}
    assert mac_coder.message_in("dsh", json.dumps(done)) == "All done"
    failed = {
        "type": "final",
        "text": "",
        "reason": {"kind": "error", "error": {"code": "MISSING_CREDENTIAL", "message": "no key"}},
    }
    assert mac_coder.message_in("dsh", json.dumps(failed)) == "MISSING_CREDENTIAL: no key"


def test_sign_in_is_read_from_each_clis_own_status() -> None:
    assert mac_coder.signed_in("claude_code", '{"loggedIn": true}', 0)
    assert not mac_coder.signed_in("claude_code", '{"loggedIn": false}', 0)
    assert mac_coder.signed_in("codex", "Logged in using ChatGPT", 0)
    assert not mac_coder.signed_in("codex", "Not logged in", 1)


def test_deepseek_harness_is_ready_with_a_deepseek_key_for_its_headless_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    assert not mac_coder.dsh_has_key()
    (tmp_path / ".env").write_text("# DEEPSEEK_API_KEY=\nDEEPSEEK_API_KEY=\n")
    assert not mac_coder.dsh_has_key()
    (tmp_path / ".env").write_text("OTHER=1\nexport DEEPSEEK_API_KEY=sk-test\n")
    assert mac_coder.dsh_has_key()
    (tmp_path / ".env").unlink()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert mac_coder.dsh_has_key()


# ---- the folder and the worktree ----


@pytest.mark.parametrize("folder", ["../elsewhere", "/etc", ".ssh", "site/.git", ""])
def test_a_folder_outside_dev_is_refused_on_the_mac_too(root: Path, folder: str) -> None:
    with pytest.raises(Refused):
        prepare(root, job(folder=folder))


def test_a_folder_that_leads_out_of_dev_by_a_link_is_refused(root: Path, tmp_path: Path) -> None:
    (root / "away").symlink_to(tmp_path)
    with pytest.raises(Refused) as refused:
        prepare(root, job(folder="away"))
    assert refused.value.code == "outside_root"


@pytest.mark.parametrize("branch", ["main", "office/", "feature/x"])
def test_only_an_office_branch_is_made(root: Path, branch: str) -> None:
    with pytest.raises(Refused) as refused:
        prepare(root, job(branch=branch))
    assert refused.value.code == "branch_not_office"


def test_the_worktree_is_its_own_branch_and_found_again(root: Path) -> None:
    worktree, relative, made = prepare(root, job())
    assert made and relative == "office-worktrees/site/add-page"
    assert git("rev-parse", "--abbrev-ref", "HEAD", where=worktree) == "office/add-page"
    # The owner's own checkout stays where it was.
    assert git("rev-parse", "--abbrev-ref", "HEAD", where=root / "site") == "main"
    again, _, made = prepare(root, job())
    assert again == worktree and not made


def test_a_missing_folder_is_refused_unless_it_is_a_new_build(root: Path) -> None:
    with pytest.raises(Refused) as refused:
        prepare(root, job(folder="nowhere"))
    assert refused.value.code == "folder_not_found"
    worktree, _, _ = prepare(root, job(folder="office-builds/game"))
    assert (root / "office-builds" / "game" / ".git").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", where=worktree) == "office/add-page"


# ---- a whole job ----


def test_a_job_runs_the_cli_and_commits_what_it_left_on_its_branch(
    root: Path, tmp_path: Path
) -> None:
    office = Office()
    worker = Job(job(), {"claude_code": cli(tmp_path)}, send=office, raw=office.raw)
    outcome = worker.run(root)
    worktree = root / "office-worktrees/site/add-page"
    assert outcome["branch"] == "office/add-page" and outcome["exit_code"] == 0
    assert outcome["message"] == "Added page.txt" and outcome["stopped"] is False
    assert outcome["head"] != outcome["base"]
    shown = git("show", "--name-only", "--format=%s", "HEAD", where=worktree)
    assert [line for line in shown.splitlines() if line] == [
        "Office: Add a page",
        "page.txt",
    ]
    assert outcome["take_over"].endswith(f"claude --resume {worker.session}")
    assert any('"result"' in line for line in office.lines())
    # Nothing reached the owner's checkout.
    assert not (root / "site" / "page.txt").exists()
    assert git("status", "--porcelain", where=root / "site") == ""


def test_the_owners_stop_stops_the_cli_and_nothing_is_committed(root: Path, tmp_path: Path) -> None:
    office = Office([{"stop": True, "notes": []}])
    outcome = Job(job(), {"claude_code": cli(tmp_path, SLOW_CLI)}, send=office, raw=office.raw).run(
        root
    )
    assert outcome["stopped"] is True
    assert outcome["head"] == outcome["base"]


def test_a_comment_becomes_the_next_turn_on_the_same_session(root: Path, tmp_path: Path) -> None:
    office = Office([{"stop": False, "notes": ["Make it blue"]}])
    worker = Job(job(), {"claude_code": cli(tmp_path)}, send=office, raw=office.raw)
    worker.run(root)
    turns = (tmp_path / "args.log").read_text().splitlines()
    assert len(turns) == 2
    assert f"--session-id {worker.session}" in turns[0]
    assert f"--resume {worker.session}" in turns[1] and "Make it blue" in turns[1]


def test_deepseek_harness_runs_a_job_and_is_resumed_on_its_own_session(
    root: Path, tmp_path: Path
) -> None:
    office = Office([{"stop": False, "notes": ["Make it blue"]}])
    worker = Job(job(cli="dsh"), {"dsh": cli(tmp_path, FAKE_DSH)}, send=office, raw=office.raw)
    outcome = worker.run(root)
    assert worker.session == "session-abc" and outcome["message"] == "Added page.txt"
    assert outcome["exit_code"] == 0 and outcome["head"] != outcome["base"]
    turns = (tmp_path / "args.log").read_text().splitlines()
    assert len(turns) == 2 and "--session-id" not in turns[0]
    assert "--session-id session-abc" in turns[1] and "Make it blue" in turns[1]
    assert outcome["take_over"].endswith("tui --resume session-abc")


def test_a_cli_not_on_this_mac_is_refused(root: Path) -> None:
    with pytest.raises(Refused) as refused:
        Job(job(cli="codex"), {}, send=Office(), raw=Office().raw).run(root)
    assert refused.value.code == "cli_not_on_this_mac"


def test_the_branchs_start_and_end_go_to_the_office_for_its_own_checks(
    root: Path, tmp_path: Path
) -> None:
    office = Office()
    outcome = Job(job(), {"claude_code": cli(tmp_path)}, send=office, raw=office.raw).run(root)
    assert outcome["error"] == ""
    assert office.names("head") == ["README.md", "page.txt"]
    assert office.names("base") == ["README.md"]


def a_tar(files: dict[str, str]) -> bytes:
    held = io.BytesIO()
    with tarfile.open(fileobj=held, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return held.getvalue()


def test_a_build_starts_from_the_offices_version_and_sends_only_its_end(
    root: Path, tmp_path: Path
) -> None:
    office = Office(seed=a_tar({"index.html": "<p>v1</p>", "check.sh": "true"}))
    given = job(folder="office-builds/game", seed=True, base_commit="abcdef1234")
    outcome = Job(given, {"claude_code": cli(tmp_path)}, send=office, raw=office.raw).run(root)
    worktree = root / "office-worktrees/office-builds/game/add-page"
    log = git("log", "--format=%s", where=worktree).splitlines()
    assert log[:2] == ["Office: Add a page", "The office's version abcdef12"]
    assert office.names("head") == ["check.sh", "index.html", "page.txt"]
    # The office has its own copy of a build's start.
    assert "base" not in office.files
    assert outcome["error"] == ""


def test_a_comment_now_interrupts_the_turn_and_resumes_with_it(root: Path, tmp_path: Path) -> None:
    office = Office([{"stop": False, "notes": ["Stop and use red"], "now": True}])
    worker = Job(job(), {"claude_code": cli(tmp_path, SLOW_CLI)}, send=office, raw=office.raw)
    slow_then_quick = tmp_path / "turns"
    # The second turn is quick: the stand-in CLI is swapped once the first starts.
    original = mac_coder.start_turn

    def started(command: list[str], worktree: Path, errors: Path) -> Any:
        turn = original(command, worktree, errors)
        worker.binary = cli(tmp_path, FAKE_CLI)
        slow_then_quick.write_text("x")
        return turn

    mac_coder.start_turn = started  # type: ignore[assignment]
    try:
        outcome = worker.run(root)
    finally:
        mac_coder.start_turn = original  # type: ignore[assignment]
    turns = (tmp_path / "args.log").read_text().splitlines()
    assert len(turns) == 1 and "Stop and use red" in turns[0] and "--resume" in turns[0]
    assert outcome["stopped"] is False


def test_files_the_office_refuses_as_too_large_are_said_so(root: Path, tmp_path: Path) -> None:
    office = Office()

    def refuse(method: str, path: str, body: Path | None = None) -> bytes:
        raise urllib.error.HTTPError(path, 413, "too large", {}, None)  # type: ignore[arg-type]

    outcome = Job(job(), {"claude_code": cli(tmp_path)}, send=office, raw=refuse).run(root)
    assert outcome["error"] == "files_too_large"


# ---- the owner's Merge and Discard (36c) ----


def worked(root: Path, tmp_path: Path) -> tuple[Path, str]:
    """A finished job's branch: page.txt added on office/add-page."""
    office = Office()
    outcome = Job(job(), {"claude_code": cli(tmp_path)}, send=office, raw=office.raw).run(root)
    return root / "site", outcome["head"]


def test_merge_brings_the_reviewed_commit_into_the_owners_branch_and_tidies_up(
    root: Path, tmp_path: Path
) -> None:
    site, head = worked(root, tmp_path)
    done = mac_coder.merge(root, job(kind="merge", base_commit=head))
    assert done["head"] == head  # A fast-forward: exactly what was reviewed.
    assert (site / "page.txt").read_text() == "hello\n"
    assert not (root / "office-worktrees/site/add-page").exists()
    assert git("branch", "--list", "office/add-page", where=site) == ""


def test_merge_refuses_a_branch_moved_since_the_review(root: Path, tmp_path: Path) -> None:
    site, _ = worked(root, tmp_path)
    with pytest.raises(Refused) as refused:
        mac_coder.merge(root, job(kind="merge", base_commit="0" * 40))
    assert refused.value.code == "branch_moved"
    assert not (site / "page.txt").exists()


def test_merge_refuses_a_checkout_with_uncommitted_changes(root: Path, tmp_path: Path) -> None:
    site, head = worked(root, tmp_path)
    (site / "README.md").write_text("my own edit\n")
    with pytest.raises(Refused) as refused:
        mac_coder.merge(root, job(kind="merge", base_commit=head))
    assert refused.value.code == "checkout_not_clean"
    assert (site / "README.md").read_text() == "my own edit\n"


def test_merge_refuses_a_checkout_on_another_branch(root: Path, tmp_path: Path) -> None:
    site, head = worked(root, tmp_path)
    git("checkout", "-q", "-b", "mine", where=site)
    with pytest.raises(Refused) as refused:
        mac_coder.merge(root, job(kind="merge", base_commit=head))
    assert refused.value.code == "checkout_not_on_base"


def test_a_conflicting_merge_changes_nothing(root: Path, tmp_path: Path) -> None:
    site, head = worked(root, tmp_path)
    (site / "page.txt").write_text("the owner's own page\n")
    git("add", "-A", where=site)
    git(*mac_coder.IDENTITY, "commit", "-q", "-m", "mine", where=site)
    before = git("rev-parse", "HEAD", where=site)
    with pytest.raises(Refused) as refused:
        mac_coder.merge(root, job(kind="merge", base_commit=head))
    assert refused.value.code == "merge_conflict"
    assert git("rev-parse", "HEAD", where=site) == before
    assert git("status", "--porcelain", where=site) == ""


def test_discard_removes_only_the_jobs_worktree_and_branch(root: Path, tmp_path: Path) -> None:
    site, _ = worked(root, tmp_path)
    mac_coder.discard(root, job(kind="discard"))
    assert not (root / "office-worktrees/site/add-page").exists()
    assert git("branch", "--list", "office/add-page", where=site) == ""
    assert git("rev-parse", "--abbrev-ref", "HEAD", where=site) == "main"
    with pytest.raises(Refused):
        mac_coder.discard(root, job(kind="discard", branch="main"))
