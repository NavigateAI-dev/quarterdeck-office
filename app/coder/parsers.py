"""Each coding CLI's output, as the steps a task shows.

The helper on the Mac sends the CLI's own output lines unchanged; the office
reads them here, so the helper stays small and a parser fix needs no new
helper. Claude Code's and Codex's readers are the ones the office used when it
ran them itself (Stage 36e moved them here).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

Describe = Callable[[str, Path], list[dict[str, Any]]]
STEP_TEXT = 400
FILE_TOOLS = {"Write": "Created", "Edit": "Edited", "MultiEdit": "Edited"}


def _relative(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return Path(path).name


def _command_text(command: str) -> str:
    """`/bin/sh -lc 'node --check app.js'` as `node --check app.js`."""
    match = re.match(r"^\S*sh -l?c (['\"])(.*)\1$", command.strip(), re.DOTALL)
    return (match.group(2) if match else command).strip()[:200]


def _plan_items(given: Any) -> list[dict[str, Any]]:
    """The agent's to-do list: each line, and whether it is done."""
    items = []
    for entry in given or []:
        if isinstance(entry, dict) and str(entry.get("text") or "").strip():
            items.append(
                {"text": str(entry["text"]).strip()[:200], "done": bool(entry.get("completed"))}
            )
    return items[:30]


def describe_codex(line: str, root: Path) -> list[dict[str, Any]]:
    """One line of Codex's event stream, as steps the owner can read.

    Finished items are the record: notes, files, commands. The rest is the
    live view: the session it can be resumed by, what it is doing right
    now, its own plan as it ticks it off, and its short reasoning.
    """
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    kind_of_event = event.get("type")
    if kind_of_event == "thread.started" and event.get("thread_id"):
        return [{"kind": "session", "id": str(event["thread_id"])[:80]}]
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return []
    kind = item.get("type")
    if kind == "todo_list" and kind_of_event in ("item.started", "item.updated", "item.completed"):
        return [{"kind": "plan", "items": _plan_items(item.get("items"))}]
    if kind_of_event == "item.started":
        if kind == "command_execution":
            return [
                {
                    "kind": "now",
                    "text": f"Running {_command_text(str(item.get('command') or ''))}",
                    "item": str(item.get("id") or ""),
                }
            ]
        return []
    if kind_of_event != "item.completed":
        return []
    if kind == "agent_message" and str(item.get("text") or "").strip():
        return [{"kind": "note", "text": str(item["text"]).strip()[:400]}]
    if kind == "reasoning" and str(item.get("text") or "").strip():
        return [{"kind": "thinking", "text": str(item["text"]).strip()[:300]}]
    if kind == "file_change":
        verbs = {"add": "Created", "update": "Edited", "delete": "Removed"}
        return [
            {
                "kind": "file",
                "text": f"{verbs.get(str(change.get('kind')), 'Changed')} "
                f"{_relative(str(change.get('path') or ''), root)}",
                "path": _relative(str(change.get("path") or ""), root),
            }
            for change in item.get("changes") or []
            if isinstance(change, dict)
        ]
    if kind == "command_execution":
        code = item.get("exit_code")
        return [
            {
                "kind": "command",
                "text": f"Ran {_command_text(str(item.get('command') or ''))}",
                "ok": code == 0,
            }
        ]
    return []


def describe_claude(line: str, root: Path) -> list[dict[str, Any]]:
    """One line of Claude Code's stream, as steps the owner can read."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    if event.get("type") == "system" and event.get("subtype") == "init":
        # The session a comment resumes.
        found = str(event.get("session_id") or "")
        return [{"kind": "session", "id": found[:80]}] if found else []
    if event.get("type") != "assistant":
        return []
    steps: list[dict[str, Any]] = []
    for part in (event.get("message") or {}).get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            steps.append({"kind": "note", "text": str(part["text"]).strip()[:400]})
        elif part.get("type") == "thinking" and str(part.get("thinking") or "").strip():
            steps.append({"kind": "thinking", "text": str(part["thinking"]).strip()[:300]})
        elif part.get("type") == "tool_use" and part.get("name") == "TodoWrite":
            todos = (part.get("input") or {}).get("todos") or []
            steps.append(
                {
                    "kind": "plan",
                    "items": [
                        {
                            "text": str(todo.get("content") or "")[:200],
                            "done": todo.get("status") == "completed",
                        }
                        for todo in todos
                        if isinstance(todo, dict) and todo.get("content")
                    ][:30],
                }
            )
        elif part.get("type") == "tool_use" and part.get("name") in FILE_TOOLS:
            target = str((part.get("input") or {}).get("file_path") or "")
            try:
                shown = str(Path(target).resolve().relative_to(root.resolve()))
            except (ValueError, OSError):
                shown = Path(target).name
            steps.append(
                {"kind": "file", "text": f"{FILE_TOOLS[part['name']]} {shown}", "path": shown}
            )
    return steps


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def describe_opencode(line: str, root: Path) -> list[dict[str, Any]]:
    """`opencode run --format json`: its text parts and the files it wrote.

    Read loosely: the format was not checked against a running opencode yet."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    part = _dict(event.get("part")) or event
    if event.get("sessionID") or part.get("sessionID"):
        found = str(event.get("sessionID") or part.get("sessionID"))[:80]
        steps: list[dict[str, Any]] = [{"kind": "session", "id": found}]
    else:
        steps = []
    if part.get("type") == "text" and str(part.get("text") or "").strip():
        steps.append({"kind": "note", "text": str(part["text"]).strip()[:STEP_TEXT]})
    elif part.get("type") == "tool" and part.get("tool") in ("write", "edit"):
        state = _dict(part.get("state"))
        target = str((state.get("input") or {}).get("filePath") or "")
        try:
            shown = str(Path(target).relative_to(root))
        except ValueError:
            shown = Path(target).name
        if shown:
            verb = "Created" if part["tool"] == "write" else "Edited"
            steps.append({"kind": "file", "text": f"{verb} {shown}", "path": shown})
    return steps


DSH_FILE_TOOLS = {"write": "Created", "edit": "Edited"}


def describe_dsh(line: str, root: Path) -> list[dict[str, Any]]:
    """`dsh --profile headless --json`: DeepSeek Harness's run events.

    Its stream opens with `session` and closes with `final`; `text` and
    `thinking` arrive as each step commits, `tool_call` as each tool starts."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    said = str(event.get("text") or "").strip()
    if kind == "session" and event.get("sessionId"):
        return [{"kind": "session", "id": str(event["sessionId"])[:80]}]
    if kind == "text" and said:
        return [{"kind": "note", "text": said[:STEP_TEXT]}]
    if kind == "thinking" and said:
        return [{"kind": "thinking", "text": said[:300]}]
    if kind == "final":
        error = _dict(_dict(event.get("reason")).get("error"))
        if not error:
            return []
        why = ": ".join(str(error[key]) for key in ("code", "message") if error.get(key))
        return [
            {"kind": "phase", "text": f"DeepSeek Harness stopped: {why}"[:STEP_TEXT], "ok": False}
        ]
    if kind != "tool_call":
        return []
    tool = str(event.get("tool") or "")
    given = _dict(event.get("input"))
    if tool == "todo_write":
        items = [
            {
                "text": str(todo.get("content") or "")[:200],
                "done": todo.get("status") == "completed",
            }
            for todo in given.get("todos") or []
            if isinstance(todo, dict) and todo.get("content")
        ]
        return [{"kind": "plan", "items": items[:30]}]
    if tool in DSH_FILE_TOOLS and given.get("file_path"):
        target = str(given["file_path"])
        shown = _relative(target, root) if Path(target).is_absolute() else target
        return [{"kind": "file", "text": f"{DSH_FILE_TOOLS[tool]} {shown}", "path": shown}]
    if tool == "bash" and given.get("command"):
        return [
            {
                "kind": "now",
                "text": f"Running {_command_text(str(given['command']))}",
                "item": str(event.get("callId") or ""),
            }
        ]
    return []


PARSERS: dict[str, Describe] = {
    "claude_code": describe_claude,
    "codex": describe_codex,
    "opencode": describe_opencode,
    "dsh": describe_dsh,
}


def steps_of(cli: str, lines: list[str], root: Path) -> list[dict[str, Any]]:
    """The steps in these lines; a CLI without a reader shows none."""
    describe = PARSERS.get(cli)
    if describe is None:
        return []
    steps: list[dict[str, Any]] = []
    for line in lines:
        steps.extend(describe(line, root))
    return steps
