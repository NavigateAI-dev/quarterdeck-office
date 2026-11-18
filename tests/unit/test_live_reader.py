"""A space's folder read live, as it is now."""

from pathlib import Path

from app.sources.reader import READ_CHARS, live_files, live_read, live_search


def folder(tmp_path: Path) -> Path:
    mount = tmp_path / "dev"
    site = mount / "acme" / "site"
    (site / "docs").mkdir(parents=True)
    (site / "docs" / "pricing.md").write_text(
        "# Pricing\nPilot tier: EUR 450 a month\nSupport by mail\n"
    )
    (site / "notes.txt").write_text("call Mark about the pilot\n")
    (site / ".env").write_text("SECRET=1\n")
    (site / "id_rsa").write_text("key\n")
    (site / "image.png").write_bytes(b"\x89PNG")
    (mount / "outside.md").write_text("pilot tier outside the folder\n")
    (site / "escape.md").symlink_to(mount / "outside.md")
    return mount


def test_a_file_written_after_the_grant_is_found(tmp_path: Path) -> None:
    mount = folder(tmp_path)
    (mount / "acme" / "site" / "new.md").write_text("The pilot tier changed today.\n")

    found = live_search(mount, "acme/site", "pilot tier")

    assert found["error"] is None
    assert {(hit["path"], hit["line"]) for hit in found["hits"]} == {
        ("docs/pricing.md", 2),
        ("new.md", 1),
    }


def test_a_symlink_out_of_the_folder_a_hidden_or_key_file_is_never_read(tmp_path: Path) -> None:
    mount = folder(tmp_path)

    listed = [row["path"] for row in live_files(mount, "acme/site")["files"]]

    assert sorted(listed) == ["docs/pricing.md", "notes.txt"]
    assert live_read(mount, "acme/site", "escape.md")["error"] == "outside_root"
    assert live_read(mount, "acme/site", ".env")["error"] == "hidden_file"
    assert live_read(mount, "acme/site", "id_rsa")["error"] == "sensitive_file"
    assert live_read(mount, "acme/site", "image.png")["error"] == "unsupported_format"
    assert live_read(mount, "acme/site", "../outside.md")["error"] == "outside_root"
    assert live_read(mount, "../etc", "passwd")["error"] == "root_not_found"


def test_a_long_file_is_read_a_part_at_a_time(tmp_path: Path) -> None:
    mount = folder(tmp_path)
    (mount / "acme" / "site" / "long.txt").write_text("x" * (READ_CHARS + 10))

    first = live_read(mount, "acme/site", "long.txt")
    rest = live_read(mount, "acme/site", "long.txt", offset=READ_CHARS)

    assert (len(first["text"]), first["more"], first["total_chars"]) == (
        READ_CHARS,
        True,
        READ_CHARS + 10,
    )
    assert (len(rest["text"]), rest["more"]) == (10, False)


def test_an_empty_search_and_a_missing_folder_say_so(tmp_path: Path) -> None:
    mount = folder(tmp_path)

    assert live_search(mount, "acme/site", "  ")["error"] == "empty_query"
    assert live_files(mount, "acme/none")["error"] == "root_not_found"


async def test_the_office_asks_the_reader_through_the_exchange(tmp_path: Path) -> None:
    """The api side and the reader meet only in the exchange folder, as in the containers."""
    import asyncio

    from app.sources.client import ExchangeIndexer
    from app.sources.reader import serve_once

    mount = folder(tmp_path)
    exchange = tmp_path / "exchange"
    (exchange / "jobs").mkdir(parents=True)
    stop = asyncio.Event()

    async def reader() -> None:
        while not stop.is_set():
            if not await asyncio.to_thread(serve_once, mount, exchange):
                await asyncio.sleep(0.01)

    serving = asyncio.create_task(reader())
    client = ExchangeIndexer(exchange, poll=0.01)
    try:
        found = await client.search("acme/site", "pilot")
        read = await client.read("acme/site", "notes.txt")
        listed = await client.files("acme/site")
    finally:
        stop.set()
        await serving
    assert {hit["path"] for hit in found["hits"]} == {"docs/pricing.md", "notes.txt"}
    assert read["text"].startswith("call Mark")
    assert len(listed["files"]) == 2
