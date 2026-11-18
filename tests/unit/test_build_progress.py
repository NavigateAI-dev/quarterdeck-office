"""A build's progress, live, and stopping it."""

import json
import sys
from pathlib import Path

import pytest

from app.coder.parsers import describe_claude, describe_codex, describe_dsh, steps_of
from app.runtimes.base import ExecutionError, ExecutionLimits, run_bounded

ROOT = Path("/work/build")


def codex(event: dict) -> str:
    return json.dumps(event)


def test_codex_file_changes_read_as_files_in_the_build() -> None:
    line = codex(
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [
                    {"path": "/work/build/notes.txt", "kind": "add"},
                    {"path": "/work/build/css/site.css", "kind": "update"},
                ],
                "status": "completed",
            },
        }
    )
    assert describe_codex(line, ROOT) == [
        {"kind": "file", "text": "Created notes.txt", "path": "notes.txt"},
        {"kind": "file", "text": "Edited css/site.css", "path": "css/site.css"},
    ]


def test_codex_commands_read_without_their_shell_wrapper() -> None:
    line = codex(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/bin/sh -lc 'node --check app.js'",
                "exit_code": 1,
                "status": "completed",
            },
        }
    )
    assert describe_codex(line, ROOT) == [
        {"kind": "command", "text": "Ran node --check app.js", "ok": False}
    ]


def test_codex_notes_and_noise() -> None:
    note = codex(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Writing the page."}}
    )
    assert describe_codex(note, ROOT) == [{"kind": "note", "text": "Writing the page."}]
    for noise in (
        codex({"type": "item.started", "item": {"type": "agent_message"}}),
        codex({"type": "turn.completed", "usage": {}}),
        "Reading additional input from stdin...",
        "{not json",
    ):
        assert describe_codex(noise, ROOT) == []


def test_claude_codes_file_tools_and_text_read_as_steps() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll write the page."},
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/work/build/index.html"},
                    },
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        }
    )
    assert describe_claude(line, ROOT) == [
        {"kind": "note", "text": "I'll write the page."},
        {"kind": "file", "text": "Created index.html", "path": "index.html"},
    ]


def test_deepseek_harness_events_read_as_steps() -> None:
    events = [
        {"type": "session", "sessionId": "session-abc", "cwd": "/work/build"},
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "thinking", "text": "Look at the layout first."},
        {"type": "text", "text": "I'll write the page."},
        {
            "type": "tool_call",
            "callId": "c1",
            "tool": "todo_write",
            "input": {
                "todos": [
                    {"content": "Write the page", "status": "completed"},
                    {"content": "Run the tests", "status": "in_progress"},
                ]
            },
        },
        {
            "type": "tool_call",
            "callId": "c2",
            "tool": "write",
            "input": {"file_path": "/work/build/index.html", "content": "x"},
        },
        {"type": "tool_call", "callId": "c3", "tool": "edit", "input": {"file_path": "src/app.py"}},
        {
            "type": "tool_call",
            "callId": "c4",
            "tool": "bash",
            "input": {"command": "bash check.sh"},
        },
        {"type": "tool_result", "callId": "c4", "status": "completed", "result": "ok"},
        {"type": "final", "sessionId": "session-abc", "reason": {"kind": "completed"}, "text": "x"},
    ]
    steps = steps_of("dsh", [json.dumps(event) for event in events], ROOT)
    assert steps == [
        {"kind": "session", "id": "session-abc"},
        {"kind": "thinking", "text": "Look at the layout first."},
        {"kind": "note", "text": "I'll write the page."},
        {
            "kind": "plan",
            "items": [
                {"text": "Write the page", "done": True},
                {"text": "Run the tests", "done": False},
            ],
        },
        {"kind": "file", "text": "Created index.html", "path": "index.html"},
        {"kind": "file", "text": "Edited src/app.py", "path": "src/app.py"},
        {"kind": "now", "text": "Running bash check.sh", "item": "c4"},
    ]


def test_a_deepseek_harness_failure_is_said_plainly() -> None:
    failed = {
        "type": "final",
        "text": "",
        "reason": {"kind": "error", "error": {"code": "MISSING_CREDENTIAL", "message": "no key"}},
    }
    assert describe_dsh(json.dumps(failed), ROOT) == [
        {
            "kind": "phase",
            "text": "DeepSeek Harness stopped: MISSING_CREDENTIAL: no key",
            "ok": False,
        }
    ]
    assert describe_dsh("not json", ROOT) == []


async def test_each_line_is_reported_as_it_arrives(tmp_path: Path) -> None:
    lines: list[str] = []
    script = (
        "import sys,time\nfor i in range(3):\n print(f'line {i}', flush=True)\n time.sleep(0.05)"
    )
    result = await run_bounded(
        [sys.executable, "-c", script], tmp_path, ExecutionLimits(on_line=lines.append)
    )
    assert result.succeeded and lines == ["line 0", "line 1", "line 2"]


async def test_a_command_is_stopped_when_the_owner_asks(tmp_path: Path) -> None:
    asked = {"stop": False}
    lines: list[str] = []

    def heard(line: str) -> None:
        lines.append(line)
        asked["stop"] = True

    script = "import time\nprint('started', flush=True)\ntime.sleep(60)"
    with pytest.raises(ExecutionError) as stopped:
        await run_bounded(
            [sys.executable, "-c", script],
            tmp_path,
            ExecutionLimits(
                on_line=heard, should_stop=lambda: asked["stop"], stop_poll_seconds=0.2
            ),
        )
    assert stopped.value.code == "stopped_by_owner" and lines == ["started"]


async def test_a_broken_view_never_fails_the_command(tmp_path: Path) -> None:
    def broken(line: str) -> None:
        raise RuntimeError("view gone")

    result = await run_bounded(
        [sys.executable, "-c", "print('x')"], tmp_path, ExecutionLimits(on_line=broken)
    )
    assert result.succeeded


async def test_a_command_never_waits_for_input(tmp_path: Path) -> None:
    """Codex reads stdin while it is open; a command gets none, so none blocks."""
    result = await run_bounded(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
        tmp_path,
        ExecutionLimits(timeout_seconds=10),
    )
    assert result.succeeded and result.stdout.strip() == "0"


def test_a_double_quoted_shell_wrapper_is_stripped_too() -> None:
    line = codex(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": '/bin/sh -lc "sh check.sh && git status --short"',
                "exit_code": 0,
            },
        }
    )
    assert describe_codex(line, ROOT)[0]["text"] == "Ran sh check.sh && git status --short"


def test_codex_says_what_it_is_doing_now_and_its_plan() -> None:
    """LB2: the middle of a build, not only what finished."""
    started = codex(
        {
            "type": "item.started",
            "item": {
                "id": "item_3",
                "type": "command_execution",
                "command": "/bin/sh -lc 'npm test'",
            },
        }
    )
    assert describe_codex(started, ROOT) == [
        {"kind": "now", "text": "Running npm test", "item": "item_3"}
    ]
    plan = codex(
        {
            "type": "item.updated",
            "item": {
                "type": "todo_list",
                "items": [
                    {"text": "Write the page", "completed": True},
                    {"text": "Add printing", "completed": False},
                ],
            },
        }
    )
    assert describe_codex(plan, ROOT) == [
        {
            "kind": "plan",
            "items": [
                {"text": "Write the page", "done": True},
                {"text": "Add printing", "done": False},
            ],
        }
    ]
    thought = codex(
        {"type": "item.completed", "item": {"type": "reasoning", "text": "The grid needs gaps."}}
    )
    assert describe_codex(thought, ROOT) == [{"kind": "thinking", "text": "The grid needs gaps."}]
    session = codex({"type": "thread.started", "thread_id": "01a1-thread"})
    assert describe_codex(session, ROOT) == [{"kind": "session", "id": "01a1-thread"}]
