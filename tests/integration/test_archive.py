"""AI history: imported through the reader, kept once, searched, deleted for good."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.archive import service
from app.archive.tools import archive_specs
from app.db.models import (
    ArchiveConversation,
    ArchiveImport,
    ArchiveMemory,
    ArchiveMessage,
    RoomMessage,
    Topic,
)
from app.office_settings import spent_this_month
from app.sources.reader import ARCHIVE, serve_once
from tests.integration.api_support import new_agent

pytestmark = pytest.mark.integration


def _claude(uuid: str, name: str, *said: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "name": name,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T04:00:00Z",
        "chat_messages": [
            {"uuid": f"{uuid}-{n}", "sender": who, "text": words, "created_at": at}
            for n, (who, words, at) in enumerate(said)
        ],
    }


def _chatgpt(ident: str, title: str, words: str) -> dict[str, Any]:
    message = {
        "id": f"{ident}-m",
        "author": {"role": "user"},
        "create_time": 1_780_000_000,
        "content": {"parts": [words]},
    }
    return {
        "conversation_id": ident,
        "title": title,
        "current_node": "n",
        "mapping": {"n": {"id": "n", "parent": None, "message": message}},
    }


EXPORT = [
    _claude(
        "k1",
        "Regulation",
        ("human", "我们需要 ABC 牌照吗", "2026-01-02T03:04:05Z"),
        ("assistant", "Probably an ABC licence, yes.", "2026-03-02T03:04:09Z"),
    ),
    _chatgpt("c1", "Orchestration", "payment orchestration in 100% of flows"),
]


class _Reader:
    """Does what the reader service does, on the same exchange folder."""

    def __init__(self, exchange: Path) -> None:
        self.exchange = exchange

    async def parse_archive(self, name: str) -> dict[str, Any]:
        from app.archive.adapters import parse_export

        folder = self.exchange / ARCHIVE
        return parse_export(folder / f"{name}.upload", folder / f"{name}.jsonl")


@pytest.fixture
def exchange(client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    monkeypatch.setattr(settings, "sources_exchange", tmp_path)
    monkeypatch.setattr(
        "app.api.routers.archive.build_indexer", lambda _settings: _Reader(tmp_path)
    )
    return tmp_path


async def _import(client: httpx.AsyncClient, data: Any) -> dict[str, Any]:
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    sent = await client.post(
        "/archive/imports",
        params={"filename": "conversations.json"},
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert sent.status_code == 202, sent.text
    imports = (await client.get("/archive")).json()["imports"]
    return next(row for row in imports if row["id"] == sent.json()["id"])


async def test_importing_twice_adds_nothing_and_cleans_up(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    first = await _import(client, EXPORT)
    assert first["status"] == "done"
    assert (first["conversations_new"], first["messages_new"]) == (2, 3)
    again = await _import(client, EXPORT)
    assert (again["conversations_new"], again["messages_new"]) == (0, 0)
    assert again["conversations_seen"] == 2
    assert list((exchange / ARCHIVE).iterdir()) == []
    sources = (await client.get("/archive")).json()["sources"]
    assert {s["provider"]: s["messages"] for s in sources} == {"claude": 2, "chatgpt": 1}


async def test_a_longer_export_adds_only_the_new_message(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    await _import(client, EXPORT)
    longer = _claude(
        "k1",
        "Regulation",
        ("human", "我们需要 ABC 牌照吗", "2026-01-02T03:04:05Z"),
        ("assistant", "Probably an ABC licence, yes.", "2026-03-02T03:04:09Z"),
        ("human", "Thanks", "2026-03-03T03:04:09Z"),
    )
    assert (await _import(client, [longer]))["messages_new"] == 1


async def test_a_bad_file_fails_with_its_reason_and_leaves_nothing(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    done = await _import(client, b'{"not": "a list"}')
    assert (done["status"], done["error"]) == ("failed", "not_a_conversation_list")
    assert list((exchange / ARCHIVE).iterdir()) == []


async def test_text_postgres_refuses_is_cleaned_not_fatal(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    raw = json.dumps(
        [_claude("k9", "Odd\u0000", ("human", "nul\u0000 and \ud83d half", "2026-01-01T00:00:00Z"))]
    ).encode()
    done = await _import(client, raw)
    assert done["status"] == "done" and done["messages_new"] == 1


async def test_search_finds_chinese_and_filters_and_traces_to_the_message(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    await _import(client, EXPORT)

    async def hits(**params: Any) -> list[dict[str, Any]]:
        found = await client.get("/archive/search", params=params)
        assert found.status_code == 200
        return list(found.json()["hits"])

    chinese = await hits(q="牌照")
    assert [h["title"] for h in chinese] == ["Regulation"] and chinese[0]["role"] == "user"
    assert len(await hits(q="ABC")) == 2
    assert len(await hits(q="ABC 牌照")) == 1, "every word must appear"
    assert await hits(q="ABC", source="chatgpt") == []
    assert await hits(q="100%") != [], "a percent sign is only a character"
    assert await hits(q="1%") == []
    assert [h["role"] for h in await hits(q="ABC", since="2026-02-01T00:00:00Z")] == ["assistant"]
    assert [h["role"] for h in await hits(q="ABC", until="2026-02-01T00:00:00Z")] == ["user"]
    hit = (await hits(q="licence"))[0]
    opened = await client.get(
        f"/archive/conversations/{hit['conversation']}", params={"around": hit["message_id"]}
    )
    assert hit["message_id"] in [m["id"] for m in opened.json()["messages"]]


async def test_search_also_finds_the_offices_own_discussions(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        topic = Topic(title="Here")
        session.add(topic)
        session.flush()
        session.add(
            RoomMessage(
                topic_id=topic.id, author_kind="owner", author_name="Me", body="our 费率 question"
            )
        )
        topic_id = topic.id
    found = (await client.get("/archive/search", params={"q": "费率"})).json()["hits"]
    assert found[0]["source"] == "office" and found[0]["conversation"] == f"office:{topic_id}"
    opened = await client.get(f"/archive/conversations/{found[0]['conversation']}")
    assert opened.json()["messages"][0]["content"] == "our 费率 question"


async def test_a_hidden_conversation_reaches_no_agent_but_stays_on_the_page(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    await _import(client, EXPORT)
    specs = {spec.name: spec for spec in archive_specs(factory)}
    assert "Nothing found" in (await specs["search_memory"].run({"query": "牌照"})).text
    imported = (await client.get("/archive")).json()["imports"][0]
    assert imported["hidden_ready"] == 2
    opened = await client.post(f"/archive/imports/{imported['id']}/open")
    assert opened.json() == {"opened": 2}
    seen = (await specs["search_memory"].run({"query": "牌照"})).text
    assert "牌照" in seen and "an AI's suggestion" in seen and "the owner said" in seen
    with factory() as session:
        row = session.scalar(
            select(ArchiveConversation).where(ArchiveConversation.external_id == "k1")
        )
        ident = row.id
    assert (
        await client.put(f"/archive/conversations/{ident}", json={"excluded": True})
    ).status_code == 200
    assert "Nothing found" in (await specs["search_memory"].run({"query": "牌照"})).text
    read = await specs["get_conversation"].run({"conversation": f"archive:{ident}"})
    assert "Not read" in read.text
    assert (await client.get("/archive/search", params={"q": "牌照"})).json()["hits"][0]["excluded"]


async def test_deleted_is_gone_and_an_old_export_cannot_bring_it_back(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    await _import(client, EXPORT)
    with factory() as session:
        ident = session.scalar(
            select(ArchiveConversation.id).where(ArchiveConversation.external_id == "k1")
        )
    assert (await client.delete(f"/archive/conversations/{ident}")).status_code == 204
    again = await _import(client, EXPORT)
    assert again["messages_new"] == 0
    with factory() as session:
        count = session.scalar(
            select(func.count()).where(ArchiveMessage.conversation_id == UUID(str(ident)))
        )
        assert count == 0
        assert session.get(ArchiveConversation, ident).title == ""
    assert (await client.get("/archive/search", params={"q": "牌照"})).json()["hits"] == []
    assert (await client.get(f"/archive/conversations/archive:{ident}")).status_code == 404
    assert (await client.delete(f"/archive/conversations/{uuid4()}")).status_code == 404


async def test_a_stale_import_shows_as_cut_off() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    row = ArchiveImport(
        id=uuid4(), filename="x", status="running", created_at=now - timedelta(hours=2)
    )
    row.conversations_new = row.messages_new = row.conversations_seen = 0
    assert service.import_view(row, now)["error"] == "interrupted"


async def test_the_reader_service_parses_an_archive_job(tmp_path: Path) -> None:
    folder = tmp_path / ARCHIVE
    folder.mkdir()
    (folder / "abc.upload").write_text(json.dumps(EXPORT))
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "j1.job.json").write_text(json.dumps({"id": "j1", "kind": "archive", "name": "abc"}))
    assert serve_once(tmp_path / "none", tmp_path)
    assert json.loads((jobs / "j1.result.json").read_text())["conversations"] == 2
    assert len((folder / "abc.jsonl").read_text().splitlines()) == 2


async def test_the_reader_refuses_a_name_that_leaves_its_folder(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "j2.job.json").write_text(json.dumps({"id": "j2", "kind": "archive", "name": "../x"}))
    assert serve_once(tmp_path / "none", tmp_path)
    assert not (jobs / "j2.result.json").exists()


async def test_a_conversation_with_a_secret_is_held_back_and_reviewed_alone(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    risky = _claude(
        "k5", "Bank", ("human", "我的账户 NL91ABNA0417164300 费率", "2026-01-01T00:00:00Z")
    )
    await _import(
        client, [risky, _claude("k6", "Plain", ("human", "费率", "2026-01-01T00:00:00Z"))]
    )
    seen = (await client.get("/archive")).json()
    assert seen["imports"][0]["hidden_review"] == 1
    assert seen["review"][0]["title"] == "Bank"
    assert seen["review"][0]["found"] == [{"kind": "IBAN", "count": 1}]
    assert "NL91" not in json.dumps(seen), "the page says what kind, never the value"
    await client.post(f"/archive/imports/{seen['imports'][0]['id']}/open")
    specs = {spec.name: spec for spec in archive_specs(factory)}
    found = (await specs["search_memory"].run({"query": "费率"})).text
    assert "Plain" in found and "Bank" not in found
    # Opened by the owner, one by one; something new and sensitive hides it again.
    ident = seen["review"][0]["conversation"].split(":")[1]
    await client.put(f"/archive/conversations/{ident}", json={"excluded": False})
    assert "Bank" in (await specs["search_memory"].run({"query": "费率"})).text
    longer = _claude(
        "k5",
        "Bank",
        ("human", "我的账户 NL91ABNA0417164300 费率", "2026-01-01T00:00:00Z"),
        ("human", "BSN 111222333", "2026-01-02T00:00:00Z"),
    )
    await _import(client, [longer])
    assert "Bank" not in (await specs["search_memory"].run({"query": "费率"})).text


async def test_importing_again_does_not_hide_what_the_owner_opened(
    client: httpx.AsyncClient, exchange: Path
) -> None:
    factory = client.app_state.session_factory  # type: ignore[attr-defined]
    done = await _import(client, EXPORT)
    await client.post(f"/archive/imports/{done['id']}/open")
    await _import(client, EXPORT)
    specs = {spec.name: spec for spec in archive_specs(factory)}
    assert "牌照" in (await specs["search_memory"].run({"query": "牌照"})).text


class _Answer:
    def __init__(self, content: str) -> None:
        self.content = content
        self.estimated_cost = 0.25
        self.usage = type("Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()


async def _reading(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, *lines: dict[str, Any]
) -> tuple[str, list[list[dict[str, str]]]]:
    """The Regulation conversation, opened, with a model answering these lines."""
    await new_agent(client, name="Cato", role="chief")
    imported = await _import(client, EXPORT)
    await client.post(f"/archive/imports/{imported['id']}/open")
    factory = client.app_state.worker.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        row = session.scalar(
            select(ArchiveConversation).where(ArchiveConversation.external_id == "k1")
        )
        said = list(
            session.scalars(
                select(ArchiveMessage)
                .where(ArchiveMessage.conversation_id == row.id)
                .order_by(ArchiveMessage.position)
            )
        )
        conversation = str(row.id)
    sent: list[list[dict[str, str]]] = []

    async def complete(_self: Any, agent: Any, messages: Any, tools: Any = None) -> _Answer:
        sent.append(messages)
        return _Answer(
            "\n".join(json.dumps({**line, "message": said[line["message"]].id}) for line in lines)
        )

    monkeypatch.setattr("app.room_replies.RoomReplies.complete", complete)
    return conversation, sent


OWNER_LINE = {
    "kind": "question",
    "text": "Is an ABC licence needed?",
    "message": 0,
    "quote": "ABC 牌照",
}
AI_LINE = {
    "kind": "decision",
    "text": "We take an ABC licence.",
    "message": 1,
    "quote": "an ABC licence",
}


async def _read(client: httpx.AsyncClient, conversation: str) -> httpx.Response:
    return await client.post(
        f"/archive/conversations/{conversation}/memories", json={"confirm": True}
    )


async def test_an_ais_suggestion_cannot_become_the_owners_decision(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, _ = await _reading(client, monkeypatch, OWNER_LINE, AI_LINE)
    assert (await _read(client, conversation)).json()["proposed"] == 2
    proposed = (await client.get("/archive")).json()["memories"]["proposed"]
    by_text = {item["content"]: item for item in proposed}
    assert by_text["We take an ABC licence."]["kind"] == "idea"
    assert by_text["We take an ABC licence."]["said_by"] == "ai"
    assert by_text["Is an ABC licence needed?"]["said_by"] == "owner"


async def test_a_quote_its_message_does_not_hold_is_left_out(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made_up = {**OWNER_LINE, "text": "Invented", "quote": "words nobody said"}
    conversation, _ = await _reading(client, monkeypatch, made_up, OWNER_LINE)
    done = (await _read(client, conversation)).json()
    assert (done["proposed"], done["dropped"]) == (1, 1)


async def test_a_hidden_conversation_goes_to_no_model(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, sent = await _reading(client, monkeypatch, OWNER_LINE)
    await client.put(f"/archive/conversations/{conversation}", json={"excluded": True})
    refused = await _read(client, conversation)
    assert (refused.status_code, refused.json()["detail"]) == (409, "hidden")
    assert sent == []


async def test_reading_needs_a_confirmed_press(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, sent = await _reading(client, monkeypatch, OWNER_LINE)
    answer = await client.post(f"/archive/conversations/{conversation}/memories", json={})
    assert answer.status_code == 422
    assert sent == []


async def test_a_rejected_memory_stays_rejected_when_read_again(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, _ = await _reading(client, monkeypatch, OWNER_LINE)
    await _read(client, conversation)
    item = (await client.get("/archive")).json()["memories"]["proposed"][0]
    await client.put(f"/archive/memories/{item['id']}", json={"status": "rejected"})
    assert (await _read(client, conversation)).json()["proposed"] == 0
    assert (await client.get("/archive")).json()["memories"]["proposed"] == []


async def test_a_reading_counts_toward_the_months_spending(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, _ = await _reading(client, monkeypatch, OWNER_LINE)
    factory = client.app_state.worker.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        before = spent_this_month(session)
    await _read(client, conversation)
    with factory() as session:
        assert spent_this_month(session) == pytest.approx(before + 0.25)


async def test_only_kept_memories_reach_agents_and_the_export_with_their_source(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, _ = await _reading(client, monkeypatch, OWNER_LINE, AI_LINE)
    await _read(client, conversation)
    proposed = (await client.get("/archive")).json()["memories"]["proposed"]
    factory = client.app_state.worker.session_factory  # type: ignore[attr-defined]
    search = next(s for s in archive_specs(factory) if s.name == "search_memory")
    export = (await client.get("/archive/memories/export.md")).text
    assert "Nothing kept" in export

    idea = next(item for item in proposed if item["said_by"] == "ai")
    await client.put(f"/archive/memories/{idea['id']}", json={"status": "kept"})
    export = (await client.get("/archive/memories/export.md")).text
    assert "An AI said: We take an ABC licence." in export
    assert f"message {idea['message_id']}" in export
    assert "Is an ABC licence needed?" not in export
    told = (await search.run({"query": "take an ABC"})).text
    assert "kept by the owner, an AI said it" in told

    await client.put(f"/archive/conversations/{conversation}", json={"excluded": True})
    assert "We take" not in (await search.run({"query": "take an ABC"})).text
    assert "We take" not in (await client.get("/archive/memories/export.md")).text


async def test_a_correction_keeps_it_and_the_first_words(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, _ = await _reading(client, monkeypatch, OWNER_LINE)
    await _read(client, conversation)
    item = (await client.get("/archive")).json()["memories"]["proposed"][0]
    saved = await client.put(f"/archive/memories/{item['id']}", json={"content": "Better words"})
    assert saved.json()["status"] == "kept"
    assert saved.json()["corrected"] is True
    factory = client.app_state.worker.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        row = session.get(ArchiveMemory, UUID(item["id"]))
        assert row.corrected_from == "Is an ABC licence needed?"


async def test_deleting_a_conversation_deletes_its_memories(
    client: httpx.AsyncClient, exchange: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation, _ = await _reading(client, monkeypatch, OWNER_LINE)
    await _read(client, conversation)
    await client.delete(f"/archive/conversations/{conversation}")
    factory = client.app_state.worker.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        assert session.scalar(select(func.count(ArchiveMemory.id))) == 0
