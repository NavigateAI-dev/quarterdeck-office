"""Reading an AI tool's export into plain conversations, inside the `sources` service.

An export is an untrusted file (ADR 0011), so this runs where there is no network
and no key, and uses the standard library only. A new tool needs one parser and
one line in `_PARSERS`. Files can be large: conversations are read one at a time.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

CHUNK_CHARS = 1024 * 1024
# A file naming itself larger than this when unpacked is refused unread.
MAX_UNPACKED_BYTES = 8 * 1024**3
# A pasted book is kept; a single message beyond this is cut, and says so.
MAX_MESSAGE_CHARS = 1_000_000
CUT = "\n[cut at import: too long]"
ZIP_MEMBER = re.compile(r"^conversations(-\d+)?\.json$")


class NotAnExport(ValueError):
    """The file is not one this office can read; the message says why."""


def conversations_in(stream: TextIO) -> Iterator[Any]:
    """Each item of a top-level JSON list, holding one in memory at a time."""
    decoder = json.JSONDecoder()
    buffer = stream.read(CHUNK_CHARS).lstrip()
    if not buffer.startswith("["):
        raise NotAnExport("not_a_conversation_list")
    buffer = buffer[1:]
    ended = False
    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        if buffer.startswith("]"):
            return
        try:
            item, used = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            # The item runs past what is read so far.
            if ended:
                raise NotAnExport("broken_json") from None
            more = stream.read(CHUNK_CHARS)
            ended = not more
            buffer += more
            continue
        yield item
        buffer = buffer[used:]


def _moment(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def _message(external_id: Any, role: str, content: str, at: Any) -> dict[str, Any]:
    text = content.strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + CUT
    return {
        "external_id": str(external_id or ""),
        "role": role,
        "content": text,
        "sent_at": _moment(at),
    }


def _chatgpt_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    if isinstance(content.get("parts"), list):
        return "\n".join(part for part in content["parts"] if isinstance(part, str))
    return str(content["text"]) if isinstance(content.get("text"), str) else ""


def _chatgpt_path(mapping: dict[str, Any], last: Any) -> list[dict[str, Any]]:
    """The branch the conversation ended on, first message first."""
    path: list[dict[str, Any]] = []
    seen: set[str] = set()
    node = last
    while isinstance(node, str) and node in mapping and node not in seen:
        seen.add(node)
        path.append(mapping[node])
        node = mapping[node].get("parent")
    if path:
        return path[::-1]
    # No end named: everything there is, by time.
    nodes = [n for n in mapping.values() if isinstance(n, dict)]
    return sorted(nodes, key=lambda n: (n.get("message") or {}).get("create_time") or 0)


def parse_chatgpt(item: dict[str, Any]) -> dict[str, Any]:
    found = item.get("mapping")
    mapping: dict[str, Any] = found if isinstance(found, dict) else {}
    messages = []
    for node in _chatgpt_path(mapping, item.get("current_node")):
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "")
        hidden = (message.get("metadata") or {}).get("is_visually_hidden_from_conversation")
        text = _chatgpt_text(message.get("content"))
        if role in ("user", "assistant", "tool") and not hidden and text.strip():
            messages.append(
                _message(
                    message.get("id") or node.get("id"), role, text, message.get("create_time")
                )
            )
    return {
        "provider": "chatgpt",
        "external_id": str(item.get("conversation_id") or item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "started_at": _moment(item.get("create_time")),
        "updated_at": _moment(item.get("update_time")),
        "messages": messages,
    }


def _claude_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        parts = message.get("content")
        text = "\n".join(
            str(part["text"])
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    names = [
        str(part.get("file_name"))
        for part in [*(message.get("attachments") or []), *(message.get("files") or [])]
        if isinstance(part, dict) and part.get("file_name")
    ]
    return text + (f"\n[attached: {', '.join(names)}]" if names else "")


def parse_claude(item: dict[str, Any]) -> dict[str, Any]:
    messages = []
    said = item.get("chat_messages")
    for message in said if isinstance(said, list) else []:
        if not isinstance(message, dict):
            continue
        role = {"human": "user", "assistant": "assistant"}.get(str(message.get("sender")))
        text = _claude_text(message)
        if role and text.strip():
            messages.append(_message(message.get("uuid"), role, text, message.get("created_at")))
    return {
        "provider": "claude",
        "external_id": str(item.get("uuid") or ""),
        "title": str(item.get("name") or ""),
        "started_at": _moment(item.get("created_at")),
        "updated_at": _moment(item.get("updated_at")),
        "messages": messages,
    }


_PARSERS: tuple[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
    ("mapping", parse_chatgpt),
    ("chat_messages", parse_claude),
)


def parse_item(item: Any) -> dict[str, Any] | None:
    """One conversation in plain form, or None when no parser knows its shape."""
    if isinstance(item, dict):
        for key, parser in _PARSERS:
            if key in item:
                return parser(item)
    return None


def _streams(path: Path) -> Iterator[TextIO]:
    if not zipfile.is_zipfile(path):
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            yield stream
        return
    with zipfile.ZipFile(path) as archive:
        # Named members are read as streams; nothing is unpacked to a path.
        members = [
            info
            for info in archive.infolist()
            if ZIP_MEMBER.match(PurePosixPath(info.filename).name)
        ]
        if not members:
            raise NotAnExport("no_conversations_in_zip")
        if sum(info.file_size for info in members) > MAX_UNPACKED_BYTES:
            raise NotAnExport("too_large")
        for info in members:
            with archive.open(info) as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")


def _lines(items: Iterable[Any], out: TextIO) -> dict[str, Any]:
    done = {"conversations": 0, "messages": 0, "unknown": 0}
    for item in items:
        parsed = parse_item(item)
        if parsed is None:
            done["unknown"] += 1
            continue
        out.write(json.dumps(parsed) + "\n")
        done["conversations"] += 1
        done["messages"] += len(parsed["messages"])
    return done


def parse_export(path: Path, out_path: Path) -> dict[str, Any]:
    """Write one line per conversation. The result says how many, or what is wrong."""
    total = {"conversations": 0, "messages": 0, "unknown": 0}
    try:
        with out_path.open("w", encoding="utf-8") as out:
            for stream in _streams(path):
                done = _lines(conversations_in(stream), out)
                total = {key: total[key] + done[key] for key in total}
    except NotAnExport as wrong:
        return {**total, "error": str(wrong)}
    except (OSError, zipfile.BadZipFile, UnicodeError, EOFError):
        return {**total, "error": "unreadable"}
    if not total["conversations"]:
        return {**total, "error": "no_known_conversations"}
    return {**total, "error": None}
