"""A space's folders as the lead's tools."""

from typing import Any

from app.lead.checks import check_citations
from app.sources.client import SourceError
from app.workspace_tools import workspace_specs


class Reader:
    def __init__(self, fail: bool = False) -> None:
        self.asked: list[tuple[str, ...]] = []
        self.fail = fail

    async def files(self, root: str) -> dict[str, Any]:
        self.asked.append(("files", root))
        return {
            "error": None,
            "files": [{"path": "a.md", "size_bytes": 3, "modified_at": "2026-10-21T09:00"}],
        }

    async def search(self, root: str, query: str) -> dict[str, Any]:
        if self.fail:
            raise SourceError("reader_unavailable")
        self.asked.append(("search", root, query))
        return {
            "error": None,
            "hits": [{"path": "docs/pricing.md", "line": 2, "text": "Pilot: 450"}],
        }

    async def read(self, root: str, path: str, offset: int = 0) -> dict[str, Any]:
        self.asked.append(("read", root, path))
        return {"error": None, "text": "Pilot: 450", "offset": 0, "total_chars": 10, "more": False}


def tools(reader: Reader, grants: dict[str, str]) -> dict[str, Any]:
    return {spec.name: spec for spec in workspace_specs(reader, lambda: dict(grants))}


def test_a_space_without_folders_offers_no_folder_tools() -> None:
    assert tools(Reader(), {}) == {}


async def test_the_tools_reach_the_granted_folder_by_its_name_and_cite_what_they_found() -> None:
    reader = Reader()
    offered = tools(reader, {"site": "clients/acme/site"})

    found = await offered["workspace_search"].run({"folder": "site", "query": "pilot"})
    read = await offered["workspace_read"].run({"folder": "site", "path": "docs/pricing.md"})

    assert reader.asked[0] == ("search", "clients/acme/site", "pilot")
    assert "local://ws/site/docs/pricing.md#L2: Pilot: 450" in found.text
    assert read.source == "local://ws/site/docs/pricing.md"
    assert read.text.startswith("local://ws/site/docs/pricing.md")


async def test_a_folder_taken_away_mid_run_is_refused_at_its_next_use() -> None:
    grants = {"site": "clients/acme/site"}
    reader = Reader()
    offered = {spec.name: spec for spec in workspace_specs(reader, lambda: dict(grants))}
    grants.clear()

    refused = await offered["workspace_files"].run({"folder": "site"})

    assert "folder_not_granted" in refused.text and reader.asked == []


async def test_a_reader_that_does_not_answer_is_said_not_raised() -> None:
    offered = tools(Reader(fail=True), {"site": "x"})
    said = await offered["workspace_search"].run({"folder": "site", "query": "q"})
    assert "reader_unavailable" in said.text


def test_a_folder_citation_must_be_one_the_tools_returned() -> None:
    conversation = [{"role": "tool", "content": "local://ws/site/docs/pricing.md#L2: Pilot: 450"}]
    answer = (
        "Pilot is 450 (local://ws/site/docs/pricing.md). "
        "Margins are thin (local://ws/site/docs/margins.md)."
    )

    checked = check_citations(answer, conversation)

    assert checked.unresolved == ["local://ws/site/docs/margins.md"]
