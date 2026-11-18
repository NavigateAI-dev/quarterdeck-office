"""Reading exports: made-up files in the shape ChatGPT and Claude export."""

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from app.archive.adapters import conversations_in, parse_export, parse_item


def chatgpt() -> dict[str, Any]:
    def node(ident: str, parent: str | None, role: str, parts: list[Any], **more: Any) -> Any:
        message = {
            "id": ident,
            "author": {"role": role},
            "create_time": 1_760_000_000.5,
            "content": {"content_type": "text", "parts": parts},
            "metadata": more,
        }
        return {"id": ident, "parent": parent, "message": message}

    return {
        "conversation_id": "c1",
        "title": "咖啡烘焙",
        "create_time": 1_760_000_000.0,
        "update_time": 1_760_000_100.0,
        "current_node": "a2",
        "mapping": {
            "root": {"id": "root", "parent": None, "message": None},
            "s": node("s", "root", "system", ["You are helpful"]),
            "h": node("h", "s", "user", ["hidden"], is_visually_hidden_from_conversation=True),
            "u1": node("u1", "h", "user", ["什么是咖啡烘焙", {"asset": "picture"}]),
            "a1": node("a1", "u1", "assistant", ["An edited-away answer"]),
            "a2": node("a2", "u1", "assistant", ["Routing between providers"]),
        },
    }


def claude() -> dict[str, Any]:
    return {
        "uuid": "k1",
        "name": "Regulation",
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T04:00:00Z",
        "chat_messages": [
            {
                "uuid": "m1",
                "sender": "human",
                "text": "Is an EMI needed?",
                "created_at": "2026-01-02T03:04:05Z",
                "attachments": [{"file_name": "psd2.pdf"}],
            },
            {
                "uuid": "m2",
                "sender": "assistant",
                "text": "",
                "content": [{"type": "text", "text": "Usually yes."}],
                "created_at": "2026-01-02T03:04:09Z",
            },
        ],
    }


def test_chatgpt_keeps_the_branch_it_ended_on_and_what_was_shown() -> None:
    parsed = parse_item(chatgpt())
    assert parsed is not None and parsed["provider"] == "chatgpt"
    assert [(m["role"], m["content"]) for m in parsed["messages"]] == [
        ("user", "什么是咖啡烘焙"),
        ("assistant", "Routing between providers"),
    ]
    assert parsed["messages"][0]["external_id"] == "u1"
    assert parsed["started_at"].startswith("2025-10-09")


def test_claude_keeps_senders_block_text_and_attachment_names() -> None:
    parsed = parse_item(claude())
    assert parsed is not None and parsed["provider"] == "claude"
    assert parsed["messages"][0]["role"] == "user"
    assert "[attached: psd2.pdf]" in parsed["messages"][0]["content"]
    assert parsed["messages"][1]["content"] == "Usually yes."


def test_an_item_of_unknown_shape_is_not_guessed_at() -> None:
    assert parse_item({"something": "else"}) is None
    assert parse_item("words") is None


def test_items_are_read_across_chunk_boundaries() -> None:
    items = [{"n": n, "text": "字" * 3000} for n in range(5)]
    stream = io.StringIO(json.dumps(items))
    original = stream.read

    def small(_: int = -1) -> str:
        return original(700)

    stream.read = small  # type: ignore[method-assign]
    assert [item["n"] for item in conversations_in(stream)] == [0, 1, 2, 3, 4]


def _read(path: Path, tmp_path: Path) -> tuple[dict[str, Any], list[Any]]:
    out = tmp_path / "out.jsonl"
    result = parse_export(path, out)
    return result, [json.loads(line) for line in out.read_text().splitlines()]


def test_a_zip_with_split_files_and_a_bare_json_both_read(tmp_path: Path) -> None:
    packed = tmp_path / "export.zip"
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("conversations-000.json", json.dumps([chatgpt()]))
        archive.writestr("sub/conversations-001.json", json.dumps([claude()]))
        archive.writestr("users.json", json.dumps([{"mapping": {}}]))
    result, lines = _read(packed, tmp_path)
    assert result["error"] is None and result["conversations"] == 2 and result["messages"] == 4
    assert {line["provider"] for line in lines} == {"chatgpt", "claude"}
    bare = tmp_path / "conversations.json"
    bare.write_text(json.dumps([claude()]))
    assert _read(bare, tmp_path)[0]["conversations"] == 1


def test_what_is_not_an_export_says_so(tmp_path: Path) -> None:
    wrong = tmp_path / "x.json"
    for content, code in (
        ('{"a": 1}', "not_a_conversation_list"),
        ('[{"mapping": {}', "broken_json"),
        ('[{"a": 1}]', "no_known_conversations"),
    ):
        wrong.write_text(content)
        assert parse_export(wrong, tmp_path / "o.jsonl")["error"] == code
    packed = tmp_path / "e.zip"
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("notes.txt", "hello")
    assert parse_export(packed, tmp_path / "o.jsonl")["error"] == "no_conversations_in_zip"
