"""A space's folders as the lead's tools.

`workspace_files`, `workspace_search` and `workspace_read` reach the folders
given to the thread's space, through the read-only, no-network reader. The
grants are read again at every call: a folder taken away mid-run is refused at
its next use, not at the run's end. What is read is cited
`local://ws/<folder>/<path>#L<line>`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from app.sources.client import SourceError
from app.tools.pipeline import ToolResult, ToolSpec

CITED = "local://ws/{folder}/{path}"
# What a run read from a space's folder, kept as its event.
READ_EVENT = "workspace_read"


class Files(BaseModel):
    folder: str = Field(min_length=1, max_length=120)


class Search(BaseModel):
    folder: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=200)


class Read(BaseModel):
    folder: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0)


def cited(folder: str, path: str, line: int | None = None) -> str:
    address = CITED.format(folder=folder, path=path)
    return f"{address}#L{line}" if line else address


def workspace_specs(
    reader: Any,
    granted: Callable[[], dict[str, str]],
    record: Callable[[str, dict[str, Any]], Any] | None = None,
) -> list[ToolSpec]:
    """The tools, or none when the space has no folder. `granted()` gives each
    folder's name and path inside the root, as the grants stand now; `record`
    keeps what was read with the run, for the thread's Files tab (6c)."""
    named = granted()
    if not named:
        return []
    names = ", ".join(sorted(named))

    def path_of(folder: str) -> str:
        path = granted().get(folder)
        if path is None:
            raise LookupError("folder_not_granted")
        return path

    async def ask(work: Callable[[], Awaitable[ToolResult]]) -> ToolResult:
        try:
            return await work()
        except LookupError as refused:
            usable = ", ".join(sorted(granted())) or "none"
            return ToolResult(text=f"Refused: {refused}. The folders you may use: {usable}.")
        except SourceError as failed:
            return ToolResult(text=f"The folder reader did not answer: {failed.code}.")

    async def files(arguments: dict[str, Any]) -> ToolResult:
        asked = Files.model_validate(arguments)

        async def work() -> ToolResult:
            found = await reader.files(path_of(asked.folder))
            if found.get("error"):
                return ToolResult(text=f"Refused: {found['error']}.")
            rows = [
                f"{row['path']} ({row['size_bytes']} bytes, {row['modified_at'][:10]})"
                for row in found["files"]
            ]
            more = f"\n… and {found['more']} more." if found.get("more") else ""
            return ToolResult(
                text="\n".join(rows) + more if rows else "No readable files in this folder."
            )

        return await ask(work)

    async def search(arguments: dict[str, Any]) -> ToolResult:
        asked = Search.model_validate(arguments)

        async def work() -> ToolResult:
            found = await reader.search(path_of(asked.folder), asked.query)
            if found.get("error"):
                return ToolResult(text=f"Refused: {found['error']}.")
            lines = [
                f"{cited(asked.folder, hit['path'], hit['line'])}: {hit['text']}"
                for hit in found["hits"]
            ]
            pdfs = found.get("pdfs_not_searched") or 0
            note = f"\n({pdfs} PDF files not searched; read one by its path.)" if pdfs else ""
            return ToolResult(text=("\n".join(lines) or "Nothing found.") + note)

        return await ask(work)

    async def read(arguments: dict[str, Any]) -> ToolResult:
        asked = Read.model_validate(arguments)

        async def work() -> ToolResult:
            found = await reader.read(path_of(asked.folder), asked.path, asked.offset)
            if found.get("error"):
                return ToolResult(text=f"Refused: {found['error']}.")
            after = found["offset"] + len(found["text"])
            more = (
                f"\n\n[{found['total_chars'] - after} more characters: "
                f"read again with offset {after}.]"
                if found.get("more")
                else ""
            )
            address = cited(asked.folder, asked.path)
            if record is not None:
                record(READ_EVENT, {"folder": asked.folder, "path": asked.path})
            return ToolResult(text=f"{address}\n\n{found['text']}{more}", source=address)

        return await ask(work)

    return [
        ToolSpec(
            name="workspace_files",
            description=(
                f"List the readable files of one of this space's folders ({names}), newest first."
            ),
            run=files,
            input_model=Files,
            concurrency_safe=True,
        ),
        ToolSpec(
            name="workspace_search",
            description=(
                "Find lines holding every word of a query in one of this space's folders "
                f"({names}), as the files are now. Each hit is cited "
                "local://ws/<folder>/<path>#L<line>."
            ),
            run=search,
            input_model=Search,
            concurrency_safe=True,
        ),
        ToolSpec(
            name="workspace_read",
            description=(
                f"Read one file of one of this space's folders ({names}) by its path, "
                "from an offset. Cite it as local://ws/<folder>/<path>."
            ),
            run=read,
            input_model=Read,
            concurrency_safe=True,
        ),
    ]
