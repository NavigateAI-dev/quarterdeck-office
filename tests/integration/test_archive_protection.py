"""AI history: protected as the owner chose, and taken out as files."""

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.archive import protection, service
from app.archive.tools import archive_specs
from app.backup import create_backup
from app.db.models import ArchiveConversation, ArchiveImport
from app.office_settings import set_backup

pytestmark = pytest.mark.integration

CARD = "4111 1111 1111 1111"


def _factory(client: httpx.AsyncClient) -> Any:
    return client.app_state.worker.session_factory  # type: ignore[attr-defined]


def _history(client: httpx.AsyncClient, words: str = f"my card is {CARD}") -> str:
    with _factory(client)() as session, session.begin():
        made = ArchiveImport(filename="x", status="done")
        session.add(made)
        session.flush()
        service.keep(
            session,
            {
                "provider": "claude",
                "external_id": "c1",
                "title": "Cards",
                "started_at": "2026-01-02T03:04:05Z",
                "messages": [{"role": "user", "content": words, "external_id": "m1"}],
            },
            made.id,
        )
        row = session.scalar(select(ArchiveConversation))
        row.state = "active"
        return str(row.id)


async def _save(client: httpx.AsyncClient, **chosen: Any) -> httpx.Response:
    body = {"agents": "discussions", "providers": None, "backups": "encrypted", **chosen}
    return await client.put("/archive/protection", json=body)


async def test_the_defaults_keep_history_out_of_slack(client: httpx.AsyncClient) -> None:
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    with _factory(client)() as session:
        agent = {"role": "chief"}
        assert protection.may_reach(session, settings, agent, "discussion")
        assert not protection.may_reach(session, settings, agent, "slack")


@pytest.mark.parametrize(
    ("agents", "discussion", "slack"),
    [
        ("off", False, False),
        ("discussions", True, False),
        ("discussions_and_slack", True, True),
    ],
)
async def test_the_owner_chooses_where_agents_reach_it(
    client: httpx.AsyncClient, agents: str, discussion: bool, slack: bool
) -> None:
    assert (await _save(client, agents=agents)).status_code == 200
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    with _factory(client)() as session:
        agent = {"role": "chief"}
        assert protection.may_reach(session, settings, agent, "discussion") is discussion
        assert protection.may_reach(session, settings, agent, "slack") is slack


async def test_a_provider_not_ticked_gets_nothing_nor_does_one_behind_a_backup(
    client: httpx.AsyncClient,
) -> None:
    settings = client.app_state.worker.settings  # type: ignore[attr-defined]
    agent = {"connection_id": "first"}
    await _save(client, providers=["first"])
    with _factory(client)() as session:
        assert protection.provider_allowed(session, settings, agent)
    await _save(client, providers=["other"])
    with _factory(client)() as session:
        assert not protection.provider_allowed(session, settings, agent)
    await _save(client, providers=["first"])
    with _factory(client)() as session, session.begin():
        set_backup(session, "first", "second")
    with _factory(client)() as session:
        assert not protection.provider_allowed(session, settings, agent)


async def test_an_agents_search_goes_on_the_record_masked(client: httpx.AsyncClient) -> None:
    _history(client)
    search = next(s for s in archive_specs(_factory(client), "Tim") if s.name == "search_memory")
    await search.run({"query": CARD.replace(" ", "")})
    seen = (await client.get("/archive/protection")).json()["record"]
    assert seen[0]["who"] == "Tim"
    assert seen[0]["action"] == "search"
    assert CARD.replace(" ", "") not in seen[0]["detail"]


async def _unzipped(client: httpx.AsyncClient, **params: Any) -> zipfile.ZipFile:
    got = await client.get("/archive/export.zip", params=params)
    assert got.status_code == 200, got.text
    return zipfile.ZipFile(io.BytesIO(got.content))


async def test_the_export_masks_secrets_unless_asked_and_is_recorded(
    client: httpx.AsyncClient,
) -> None:
    ident = _history(client)
    bundle = await _unzipped(client)
    assert {"README.txt", "memories.json", "memories.md"} <= set(bundle.namelist())
    held = json.loads(bundle.read(f"conversations/claude-{ident}.json"))
    assert held["title"] == "Cards"
    assert CARD not in held["messages"][0]["content"]
    plain = await _unzipped(client, mask="false")
    assert (
        CARD
        in json.loads(plain.read(f"conversations/claude-{ident}.json"))["messages"][0]["content"]
    )
    assert (await client.get("/archive/protection")).json()["record"][0]["action"] == "export"


async def test_a_deleted_conversation_is_not_exported(client: httpx.AsyncClient) -> None:
    ident = _history(client)
    await client.delete(f"/archive/conversations/{ident}")
    bundle = await _unzipped(client)
    assert not [n for n in bundle.namelist() if n.startswith("conversations/")]


def _plain_in_archive(archive: Path, words: str) -> bool:
    import tarfile

    with tarfile.open(archive) as tar:
        return any(
            words.encode() in (tar.extractfile(m).read() if m.isfile() else b"")  # type: ignore[union-attr]
            for m in tar.getmembers()
        )


async def test_backups_seal_or_leave_out_history(client: httpx.AsyncClient, tmp_path: Path) -> None:
    _history(client, "the private words")
    engine = _factory(client)().get_bind()
    key = b"k" * 32
    plain = create_backup(engine, tmp_path / "none", tmp_path / "a")
    assert _plain_in_archive(plain, "the private words")
    sealed = create_backup(engine, tmp_path / "none", tmp_path / "b", history_key=key)
    assert not _plain_in_archive(sealed, "the private words")
    left = create_backup(engine, tmp_path / "none", tmp_path / "c", leave_out_history=True)
    assert not _plain_in_archive(left, "the private words")
