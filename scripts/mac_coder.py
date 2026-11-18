"""Quarterdeck Coder: the owner's own coding CLI, run on this Mac for the office.

    .venv/bin/python -m scripts.mac_coder start    # start the helper in the background
    .venv/bin/python -m scripts.mac_coder stop
    .venv/bin/python -m scripts.mac_coder status
    .venv/bin/python -m scripts.mac_coder found    # the coding CLIs it finds, and their sign-in

The office no longer runs coding agents in its container (Stage 36). This
helper asks the office every few seconds whether there is coding to do. For a
job it makes a separate git worktree on the job's `office/` branch under ~/dev
and runs the owner's CLI there (Claude Code, Codex, opencode or DeepSeek Harness), signed in with
the owner's own plan and inside the CLI's own sandbox. It sends the CLI's
output to the office as it comes, stops it when the owner presses Stop, gives
it the owner's comments as its next turn, and at the end commits what is left
on that branch and says where it is.

It never pushes, never checks out or writes the owner's own branches, never
works outside ~/dev, and never passes a CLI a flag that skips its sandbox. Its
owner token is read from the office's own .env and sent only to the office on
this Mac.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.coder.jobs import BRANCH_PREFIX, branch_refusal
from app.workspaces import refusal
from scripts.mac_calendar import BASE_URL, office, owner_token

ROOT = Path(__file__).resolve().parents[1]
VERSION = "36a.1"
CODER_ROOT = Path(os.environ.get("CODER_ROOT", "~/dev")).expanduser()
HOME = ROOT / ".coder-helper"
LOCK = HOME / "helper.lock"
LOG = HOME / "helper.log"
# The worktrees, beside the owner's folders and inside ~/dev, so the office can
# read them through its read-only share. Not a hidden folder: the office
# refuses those.
WORKTREES = "office-worktrees"
NEW_BUILDS = "office-builds"
ASK_EVERY = 5.0
AWAY_EVERY = 15.0
REPORT_EVERY = 2.0
FOUND_EVERY = 600.0
TURN_SECONDS = 3600.0
CHECK_SECONDS = 20.0
IDENTITY = ["-c", "user.name=Quarterdeck Coder", "-c", "user.email=coder@localhost"]

# DeepSeek Harness's own command, inside its desktop app; not on the path.
DSH_IN_APP = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
# Where each CLI may be, in order. `claude` is often a shell alias to the first.
CANDIDATES = {
    "claude_code": ["~/.claude/local/claude", "claude"],
    "codex": ["codex", "/opt/homebrew/bin/codex"],
    "opencode": ["~/.opencode/bin/opencode", "opencode"],
    "dsh": ["dsh", DSH_IN_APP],
}
# How each says whether it is signed in.
SIGN_IN = {
    "claude_code": ["auth", "status"],
    "codex": ["login", "status"],
    "opencode": ["auth", "list"],
}
RUNS = ("claude_code", "codex", "opencode", "dsh")
DSH_NO_KEY = (
    "No DeepSeek key for its headless mode: it does not use the app's sign-in. "
    "Put DEEPSEEK_API_KEY=... in ~/.dsh/.env."
)
# Claude Code's own sandbox (macOS Seatbelt), with shell commands allowed inside it.
CLAUDE_SANDBOX = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}})

RULES = """

---
How to work (from the Quarterdeck Coder on the owner's Mac):
- You are in a separate git worktree on the branch {branch}. Work only in this folder.
- Do not push, do not switch or make branches, do not merge. What you leave here is
  committed on {branch} for you when you finish; the office then reviews it.
- Shell commands run in your own sandbox, with no network: do not try to install
  packages. List them as the task says; the office installs them before it checks.
- When you finish, say in a few lines what you changed and how you checked it.
"""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def office_raw(method: str, path: str, body: Path | None = None) -> bytes:
    """A file to or from the office on this Mac: a build's seed, a branch's files."""
    data = body.open("rb") if body is not None else None
    headers = {"Authorization": f"Bearer {owner_token()}"}
    if body is not None:
        headers |= {"Content-Type": "application/x-tar", "Content-Length": str(body.stat().st_size)}
    request = urllib.request.Request(  # noqa: S310 - the office on this Mac
        f"{BASE_URL}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as reply:  # noqa: S310
            return bytes(reply.read())
    finally:
        if data is not None:
            data.close()


# ---- the CLIs on this Mac ----


def _binary(candidates: list[str]) -> str | None:
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if expanded.is_absolute():
            if expanded.is_file() and os.access(expanded, os.X_OK):
                return str(expanded)
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _quick(command: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(  # noqa: S603 - the CLI's own status command
            command,
            capture_output=True,
            text=True,
            timeout=CHECK_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, "did not answer"
    return done.returncode, (done.stdout + done.stderr).strip()


def signed_in(cli: str, said: str, code: int) -> bool:
    """Whether a CLI's own status command says it is signed in."""
    if code != 0:
        return False
    if cli == "claude_code":
        try:
            return bool(json.loads(said).get("loggedIn"))
        except (ValueError, AttributeError):
            return "logged in" in said.lower()
    if cli == "codex":
        return said.lower().startswith("logged in")
    return bool(said)


def dsh_has_key() -> bool:
    """Whether DeepSeek Harness's headless mode has a DeepSeek key: from the
    environment, or its own ~/.dsh/.env. Only the name is looked for; the key
    is never read out or sent anywhere."""
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return True
    home = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser()
    try:
        lines = (home / ".env").read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        name, _, value = line.strip().removeprefix("export ").partition("=")
        if name.strip() == "DEEPSEEK_API_KEY" and value.strip().strip("'\""):
            return True
    return False


def found() -> list[dict[str, Any]]:
    """Each coding CLI on this Mac: where, which version, and whether signed in."""
    clis: list[dict[str, Any]] = []
    for cli, candidates in CANDIDATES.items():
        binary = _binary(candidates)
        if binary is None:
            continue
        code, said = _quick([binary, "--version"])
        version = said.splitlines()[0][:80] if code == 0 and said else ""
        entry: dict[str, Any] = {"id": cli, "path": binary, "version": version}
        if not version:
            entry["detail"] = "Did not answer --version."
        elif cli not in RUNS:
            entry["detail"] = "Found; the coder does not run it yet."
        elif cli == "dsh":
            entry["signed_in"] = dsh_has_key()
            if not entry["signed_in"]:
                entry["detail"] = DSH_NO_KEY
        elif cli in SIGN_IN:
            code, said = _quick([binary, *SIGN_IN[cli]])
            entry["signed_in"] = signed_in(cli, said, code)
            if not entry["signed_in"]:
                entry["detail"] = "Not signed in: sign in to it in your terminal."
        clis.append(entry)
    return clis


# ---- one job ----


def folder_of(root: Path, folder: str) -> Path:
    """The owner's folder for a job, inside ~/dev, checked again on this Mac."""
    refused = refusal(folder)
    if refused:
        raise Refused(refused)
    path = (root / folder).resolve()
    if not path.is_relative_to(root.resolve()):
        raise Refused("outside_root")
    return path


def worktree_of(folder: str, branch: str) -> str:
    """Where a job's worktree goes, relative to ~/dev."""
    return f"{WORKTREES}/{folder}/{branch[len(BRANCH_PREFIX) :]}"


def _git(args: list[str], where: Path, timeout: float = 120.0) -> str:
    done = subprocess.run(  # noqa: S603 - fixed git commands
        ["git", *args],  # noqa: S607 - the Mac's own git
        cwd=where,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise Refused(f"git_{args[0]}_failed")
    return done.stdout.strip()


def _new_build(path: Path, base_branch: str) -> None:
    """A from-scratch build's own folder under ~/dev/office-builds, made once."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", base_branch], path)
    _git([*IDENTITY, "commit", "-q", "--allow-empty", "-m", "Start"], path)


def prepare(root: Path, job: dict[str, Any]) -> tuple[Path, str, bool]:
    """The job's worktree, made from its base or found again; its place; and
    whether it was made now."""
    branch = str(job.get("branch") or "")
    if branch_refusal(branch):
        raise Refused("branch_not_office")
    folder = str(job.get("folder") or "")
    repository = folder_of(root, folder)
    base_branch = str(job.get("base_branch") or "main")
    if not repository.exists():
        if not folder.startswith(f"{NEW_BUILDS}/"):
            raise Refused("folder_not_found")
        _new_build(repository, base_branch)
    if Path(_git(["rev-parse", "--show-toplevel"], repository)).resolve() != repository:
        raise Refused("not_a_repository")
    relative = worktree_of(folder, branch)
    worktree = root / relative
    if worktree.exists():
        if _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree) != branch:
            raise Refused("worktree_on_other_branch")
        return worktree, relative, False
    worktree.parent.mkdir(parents=True, exist_ok=True)
    known = subprocess.run(  # noqa: S603 - fixed git command
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],  # noqa: S607
        cwd=repository,
        check=False,
    )
    if known.returncode == 0:
        _git(["worktree", "add", "-q", str(worktree), branch], repository)
    else:
        # A build's base commit is the office's, not in this folder: it starts
        # from its own branch and is then given the office's version (seeded).
        start = base_branch if job.get("seed") else str(job.get("base_commit") or "") or base_branch
        _git(["worktree", "add", "-q", "-b", branch, str(worktree), start], repository)
    return worktree, relative, True


def _clear(worktree: Path) -> None:
    for entry in worktree.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def seeded(worktree: Path, seed: bytes, base: str) -> None:
    """A build's worktree holds exactly the office's version it builds on, so
    the office sees only the CLI's own changes."""
    _clear(worktree)
    if seed:
        with tempfile.NamedTemporaryFile(suffix=".tar") as held:
            held.write(seed)
            held.flush()
            with tarfile.open(held.name) as tar:
                tar.extractall(worktree, filter="data")
    if _git(["status", "--porcelain"], worktree):
        _git(["add", "-A"], worktree)
        _git(
            [*IDENTITY, "commit", "-q", "-m", f"The office's version {base[:8] or 'new'}"], worktree
        )


def archived(worktree: Path, commit: str, into: Path) -> Path:
    """The branch's files at a commit, as `git archive` gives them."""
    target = into / f"{commit[:12]}.tar"
    _git(["archive", "--format=tar", f"--output={target}", commit], worktree, timeout=600)
    return target


def command_for(
    cli: str, binary: str, prompt: str, session: str, resumed: bool, worktree: Path
) -> list[str]:
    """The CLI, headless, in its own sandbox; resumed on its own session."""
    if cli == "claude_code":
        command = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            # Its own sandbox, so it can run its checks headless: shell commands
            # run inside it without asking, writes stay in the worktree, and the
            # network is closed. Nothing here skips a sandbox.
            "--settings",
            CLAUDE_SANDBOX,
        ]
        return command + (["--resume", session] if resumed else ["--session-id", session])
    if cli == "codex":
        if resumed and session:
            return [
                binary,
                "exec",
                "resume",
                "--json",
                "-c",
                'sandbox_mode="workspace-write"',
                session,
                prompt,
            ]
        return [
            binary,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(worktree),
            prompt,
        ]
    if cli == "opencode":
        return [
            binary,
            "run",
            "--format",
            "json",
            *(["--session", session] if session else []),
            prompt,
        ]
    if cli == "dsh":
        # Writes only in the worktree, and nobody is there to approve more: a
        # call that would need approval is refused, never run. No extra folder
        # is granted; the helper commits what it leaves.
        return [
            binary,
            "--profile",
            "headless",
            "--json",
            "--permission-mode",
            "workspace-write",
            "--approval-policy",
            "never",
            *(["--session-id", session] if resumed and session else []),
            prompt,
        ]
    raise Refused("cli_not_supported")


def take_over(cli: str, worktree: Path, session: str, binary: str = "") -> str:
    """The command that carries a job on in the owner's own terminal."""
    where = f"cd {shlex.quote(str(worktree))} && "
    if cli == "claude_code":
        return where + f"claude --resume {shlex.quote(session)}"
    if cli == "codex":
        return where + (
            f"codex resume {shlex.quote(session)}" if session else "codex resume --last"
        )
    if cli == "opencode":
        return where + (f"opencode --session {shlex.quote(session)}" if session else "opencode")
    if cli == "dsh":
        dsh = shlex.quote(binary or "dsh")
        return where + (f"{dsh} tui --resume {shlex.quote(session)}" if session else f"{dsh} tui")
    return where.removesuffix(" && ")


def session_in(cli: str, line: str) -> str:
    """A session id in one line of output, for CLIs that choose their own."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return ""
    if not isinstance(event, dict):
        return ""
    if cli == "codex" and event.get("type") == "thread.started":
        return str(event.get("thread_id") or "")
    if cli == "opencode":
        part = _dict(event.get("part"))
        return str(event.get("sessionID") or part.get("sessionID") or "")
    if cli == "dsh" and event.get("type") == "session":
        return str(event.get("sessionId") or "")
    return ""


def message_in(cli: str, line: str) -> str:
    """What the CLI said at the end, if this line holds it."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return ""
    if not isinstance(event, dict):
        return ""
    if cli == "claude_code" and event.get("type") == "result":
        return str(event.get("result") or "")
    item = _dict(event.get("item"))
    if cli == "codex" and event.get("type") == "item.completed":
        return str(item.get("text") or "") if item.get("type") == "agent_message" else ""
    part = _dict(event.get("part"))
    if cli == "opencode" and part.get("type") == "text":
        return str(part.get("text") or "")
    if cli == "dsh" and event.get("type") == "final":
        error = _dict(_dict(event.get("reason")).get("error"))
        why = ": ".join(str(error[key]) for key in ("code", "message") if error.get(key))
        return str(event.get("text") or "") or why
    return ""


def notes_turn(notes: list[str]) -> str:
    listed = "\n".join(f"- {note}" for note in notes)
    return (
        "The owner commented while you worked. Take these into account, "
        f"carry on from where you are, and finish:\n{listed}"
    )


def commit_left(worktree: Path, brief: str) -> None:
    """Whatever the CLI left uncommitted, as one commit on the job's branch."""
    if not _git(["status", "--porcelain"], worktree):
        return
    _git(["add", "-A"], worktree)
    title = (brief.strip().splitlines() or ["Office work"])[0][:72]
    _git([*IDENTITY, "commit", "-q", "-m", f"Office: {title}"], worktree)


@dataclass
class Turn:
    """One run of the CLI, read line by line on its own thread."""

    process: subprocess.Popen[str]
    lines: queue.Queue[str] = field(default_factory=queue.Queue)
    started: float = field(default_factory=time.monotonic)
    reader: threading.Thread | None = None

    def ended(self) -> bool:
        """The CLI has exited and every line it wrote has been read."""
        reading = self.reader is not None and self.reader.is_alive()
        return self.process.poll() is not None and not reading

    def read(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))

    def taken(self) -> list[str]:
        out: list[str] = []
        while not self.lines.empty() and len(out) < 500:
            out.append(self.lines.get_nowait())
        return out

    def close(self) -> None:
        """The pipe, once the CLI is gone."""
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.stop()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def stop(self) -> None:
        """The CLI and everything it started. macOS refuses a group signal once
        its leader has exited (EPERM), so the leader is then stopped alone."""
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except PermissionError:
            self.process.terminate()
        except ProcessLookupError:
            return
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.process.kill()


def start_turn(command: list[str], worktree: Path, errors: Path) -> Turn:
    with errors.open("a") as said:  # The child keeps its own copy.
        process = subprocess.Popen(  # noqa: S603 - the owner's own CLI
            command,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=said,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    turn = Turn(process)
    turn.reader = threading.Thread(target=turn.read, daemon=True)
    turn.reader.start()
    return turn


class Job:
    """One code job, from worktree to report. `send` is the office."""

    def __init__(
        self,
        job: dict[str, Any],
        clis: dict[str, str],
        send: Any = office,
        raw: Any = office_raw,
    ) -> None:
        self.raw = raw
        self.job = job
        self.id = str(job["id"])
        self.cli = str(job.get("cli") or "")
        self.binary = clis.get(self.cli, "")
        self.session = str(job.get("session") or "")
        self.send = send
        self.unsent: list[str] = []
        self.notes: list[str] = []
        self.message = ""
        self.stopped = False

    def report(self, lines: list[str], relative: str) -> dict[str, Any]:
        """The lines so far; the office's answer, or nothing while it is away."""
        self.unsent += lines
        body = {"lines": self.unsent[:500], "session": self.session, "worktree": relative}
        try:
            answer = self.send("POST", f"/coder/jobs/{self.id}/lines", body) or {}
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return {}
        self.unsent = self.unsent[500:]
        self.notes += [str(note) for note in answer.get("notes") or []]
        return answer

    def watch(self, turn: Turn, relative: str) -> str:
        """Until the turn ends: report, stop on the owner's word or the time."""
        while True:
            ended = turn.ended()
            time.sleep(0 if ended else REPORT_EVERY)
            lines = turn.taken()
            for line in lines:
                self.session = self.session or session_in(self.cli, line)
                self.message = message_in(self.cli, line) or self.message
            answer = self.report(lines, relative)
            if answer.get("stop"):
                turn.stop()
                self.stopped = True
                return ""
            if answer.get("now") and self.notes and not ended:
                turn.stop()  # The owner's comment now: resumed with it at once.
                return ""
            if time.monotonic() - turn.started > TURN_SECONDS:
                turn.stop()
                return "timed_out"
            if ended and turn.lines.empty():
                return "" if turn.process.returncode == 0 else "cli_failed"

    def run(self, root: Path) -> dict[str, Any]:
        if self.job.get("kind") != "code":
            raise Refused("job_kind_not_built")
        if self.cli not in RUNS or not self.binary:
            raise Refused("cli_not_on_this_mac")
        worktree, relative, made = prepare(root, self.job)
        if made and self.job.get("seed"):
            seed = self.raw("GET", f"/coder/jobs/{self.id}/seed")
            seeded(worktree, seed, str(self.job.get("base_commit") or ""))
        if self.cli == "claude_code" and not self.session:
            self.session = str(uuid.uuid4())
        errors = HOME / f"job-{self.id}.stderr"
        HOME.mkdir(exist_ok=True)
        resumed = bool(self.job.get("session"))
        prompt = str(self.job.get("brief") or "") + RULES.format(branch=self.job["branch"])
        failed = ""
        while True:
            command = command_for(self.cli, self.binary, prompt, self.session, resumed, worktree)
            turn = start_turn(command, worktree, errors)
            try:
                failed = self.watch(turn, relative)
            finally:
                turn.close()
            if failed or self.stopped or not self.notes:
                break
            prompt, self.notes, resumed = notes_turn(self.notes), [], True
        if not self.stopped:
            commit_left(worktree, str(self.job.get("brief") or ""))
        base = str(self.job.get("base_commit") or "") or _git(
            ["merge-base", "HEAD", str(self.job.get("base_branch") or "main")], worktree
        )
        head = _git(["rev-parse", "HEAD"], worktree)
        if not self.stopped and not failed:
            try:
                self.send_files(worktree, head, base)
            except urllib.error.HTTPError as refused:
                failed = "files_too_large" if refused.code == 413 else "files_not_sent"
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                failed = "files_not_sent"
        return {
            "branch": self.job["branch"],
            "head": head,
            "base": base,
            "exit_code": 1 if failed == "cli_failed" else 0,
            "message": self.message[-8000:],
            "error": "" if failed in ("", "cli_failed") else failed,
            "take_over": take_over(self.cli, worktree, self.session, self.binary),
            "stopped": self.stopped,
        }

    def send_files(self, worktree: Path, head: str, base: str) -> None:
        """What the office checks: the branch's end, and for the owner's own
        folder its start too (the office has its own copy of a build's)."""
        with tempfile.TemporaryDirectory() as held:
            self.raw(
                "PUT", f"/coder/jobs/{self.id}/files/head", archived(worktree, head, Path(held))
            )
            if not self.job.get("seed"):
                self.raw(
                    "PUT", f"/coder/jobs/{self.id}/files/base", archived(worktree, base, Path(held))
                )


def _known_branch(repository: Path, branch: str) -> str:
    """The branch's commit in the owner's folder, or "" when there is none."""
    done = subprocess.run(  # noqa: S603 - fixed git command
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],  # noqa: S607
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def _remove_worktree(root: Path, repository: Path, folder: str, branch: str) -> None:
    worktree = root / worktree_of(folder, branch)
    if worktree.exists():
        _git(["worktree", "remove", "--force", str(worktree)], repository)
    _git(["worktree", "prune"], repository)


def merge(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    """The owner's Merge: the reviewed office/ branch into the base branch of
    their folder, only if their checkout is clean and on that branch. A branch
    changed since the review, or a conflict, changes nothing."""
    branch, folder = str(job.get("branch") or ""), str(job.get("folder") or "")
    if branch_refusal(branch):
        raise Refused("branch_not_office")
    repository = folder_of(root, folder)
    if not repository.exists():
        raise Refused("folder_not_found")
    reviewed = str(job.get("base_commit") or "")
    if not reviewed or _known_branch(repository, branch) != reviewed:
        raise Refused("branch_moved")
    base = str(job.get("base_branch") or "main")
    if _git(["symbolic-ref", "--short", "-q", "HEAD"], repository) != base:
        raise Refused("checkout_not_on_base")
    if _git(["status", "--porcelain", "--untracked-files=no"], repository):
        raise Refused("checkout_not_clean")
    try:
        _git(["merge", "--ff-only", "-q", branch], repository)
    except Refused:
        try:
            _git([*IDENTITY, "merge", "--no-edit", "-q", branch], repository)
        except Refused:
            subprocess.run(  # noqa: S603 - fixed git command
                ["git", "merge", "--abort"],  # noqa: S607
                cwd=repository,
                capture_output=True,
                check=False,
            )
            raise Refused("merge_conflict") from None
    head = _git(["rev-parse", "HEAD"], repository)
    _remove_worktree(root, repository, folder, branch)
    # Merged, so the branch goes too; -d refuses one that is not.
    subprocess.run(  # noqa: S603 - fixed git command
        ["git", "branch", "-d", branch],  # noqa: S607
        cwd=repository,
        capture_output=True,
        check=False,
    )
    return {"branch": branch, "head": head, "base": reviewed, "exit_code": 0}


def discard(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    """The owner's Discard: the job's worktree and its office/ branch, nothing else."""
    branch, folder = str(job.get("branch") or ""), str(job.get("folder") or "")
    if branch_refusal(branch):
        raise Refused("branch_not_office")
    repository = folder_of(root, folder)
    if not repository.exists():
        raise Refused("folder_not_found")
    _remove_worktree(root, repository, folder, branch)
    if _known_branch(repository, branch):
        _git(["branch", "-D", branch], repository)
    return {"branch": branch, "exit_code": 0}


def do_job(job: dict[str, Any], clis: dict[str, str], root: Path = CODER_ROOT) -> None:
    """One job, reported once, whatever happens to it."""
    worker = Job(job, clis)
    kind = job.get("kind")
    try:
        if kind == "merge":
            outcome = merge(root, job)
        elif kind == "discard":
            outcome = discard(root, job)
        else:
            outcome = worker.run(root)
    except Refused as refused:
        outcome = {"error": refused.code, "exit_code": None}
    except (OSError, subprocess.SubprocessError):
        outcome = {"error": "helper_failed", "exit_code": None}
    for _ in range(20):
        try:
            office("POST", f"/coder/jobs/{worker.id}/done", outcome)
            return
        except urllib.error.HTTPError:
            return  # The office refused it (closed already); nothing to retry.
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(AWAY_EVERY)


# ---- the helper ----


def run() -> int:
    HOME.mkdir(exist_ok=True)
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # Already running.
    lock.write(str(os.getpid()))
    lock.flush()
    clis: list[dict[str, Any]] = []
    looked = 0.0
    while True:
        try:
            if not clis or time.monotonic() - looked > FOUND_EVERY:
                clis, looked = found(), time.monotonic()
            hello = {"version": VERSION, "root": str(CODER_ROOT), "clis": clis}
            if (office("POST", "/coder/hello", hello) or {}).get("jobs"):
                job = (office("POST", "/coder/jobs/next") or {}).get("job")
                if job:
                    do_job(job, {cli["id"]: cli["path"] for cli in clis})
                    continue
            time.sleep(ASK_EVERY)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(AWAY_EVERY)  # The office is not up; ask again shortly.


def running() -> int | None:
    if not LOCK.exists():
        return None
    with LOCK.open() as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            text = LOCK.read_text().strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(held, fcntl.LOCK_UN)
    return None


def start() -> int:
    if sys.platform != "darwin":
        print("The coder helper is for a Mac; this computer is not one.")
        return 0
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        print("The coder helper needs the office's Python environment (.venv).")
        return 1
    if running() is not None:
        print("The coder helper is running.")
        return 0
    HOME.mkdir(exist_ok=True)
    # Started from the owner's terminal, so the CLIs find their PATH and sign-ins.
    with LOG.open("a") as log:
        subprocess.Popen(  # noqa: S603 - this module, in the office's own Python
            [str(python), "-m", "scripts.mac_coder", "run"],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    print(f"The coder helper is running. It works in {CODER_ROOT}.")
    return 0


def stop() -> int:
    pid = running()
    if pid and pid > 0:
        os.kill(pid, signal.SIGTERM)
        print("Stopped the coder helper.")
    else:
        print("The coder helper was not running.")
    return 0


def show_found() -> int:
    for cli in found():
        state = "signed in" if cli.get("signed_in") else cli.get("detail", "")
        print(f"{cli['id']}: {cli['path']} {cli['version']} ({state})")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "start"
    actions = {"start": start, "stop": stop, "run": run, "found": show_found}
    if command == "status":
        print("running" if running() is not None else "not running")
        return 0
    if command not in actions:
        print(__doc__)
        return 2
    return actions[command]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
