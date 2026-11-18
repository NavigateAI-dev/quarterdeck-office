"""Spill instead of cut.

A tool result too long for the prompt is kept whole: the model sees its head
and tail and a reference, and reads further with `read_more`, as DSH's
spill policy does. Today's tools cut at a fixed length and the rest is lost.
The store lives for one lead run; S4 keeps it in the run's event log.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.mcp.calling import ToolRefused
from app.tools.pipeline import ToolResult, ToolSpec

SPILL_CHARS = 12_000
HEAD_CHARS = 4_096
TAIL_CHARS = 1_024
READ_CHARS = 12_000


class ReadMore(BaseModel):
    ref: str
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=READ_CHARS, ge=1, le=READ_CHARS)


class Spill:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    def shown(self, text: str) -> str:
        """The text itself, or its head and tail with a reference to the rest."""
        if len(text) <= SPILL_CHARS:
            return text
        ref = f"r{len(self.stored) + 1}"
        self.stored[ref] = text
        hidden = len(text) - HEAD_CHARS - TAIL_CHARS
        return (
            f"{text[:HEAD_CHARS]}\n\n[… {hidden} characters not shown. To read them, call "
            f"read_more with ref={ref} and offset={HEAD_CHARS}.]\n\n{text[-TAIL_CHARS:]}"
        )

    def read_more_spec(self) -> ToolSpec:
        async def run(arguments: dict[str, Any]) -> ToolResult:
            text = self.stored.get(arguments["ref"])
            if text is None:
                raise ToolRefused("unknown_ref")
            start = arguments["offset"]
            return ToolResult(text=text[start : start + arguments["length"]])

        return ToolSpec(
            "read_more",
            "Read more of a long tool result that was shown only in part. Give its ref, "
            "and the character offset to start from.",
            run,
            ReadMore,
            concurrency_safe=True,
        )
