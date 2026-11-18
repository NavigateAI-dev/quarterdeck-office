"""Verify the world, not the self-report.

Before the owner reads the lead's answer, every web address it cites must be
one the tools actually returned in this run, and so must every file of a space's
folder it cites (`local://ws/…`). A delegated brief is held to the same rule: an
address its own tools did not return is taken out of its result before the lead
reads it (`keep_returned`), so a brief's word never counts as a tool's. Checked
in code, not by asking a model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

ADDRESS = re.compile(r"(?:https?|local)://[^\s<>\"'()\[\]]+")
TRAILING = ".,;:!?*_`"
REMOVED = "[address removed: no tool returned it]"


@dataclass(frozen=True)
class CitationCheck:
    cited: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved


def check_citations(
    answer: str, conversation: list[dict[str, Any]], returned_before: Iterable[str] = ()
) -> CitationCheck:
    """The addresses the answer cites, and those no tool returned."""
    seen = _returned(conversation) | {_key(address) for address in returned_before}
    cited = list(dict.fromkeys(_clean(address) for address in ADDRESS.findall(answer)))
    return CitationCheck(cited, [address for address in cited if _key(address) not in seen])


def keep_returned(
    texts: list[str], sources: list[str], conversation: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """The texts and sources with each address no tool returned taken out, and those taken."""
    seen = _returned(conversation)
    taken: list[str] = []

    def checked(match: re.Match[str]) -> str:
        address = match.group()
        if _key(address) in seen:
            return address
        taken.append(_clean(address))
        return REMOVED

    kept_texts = [ADDRESS.sub(checked, text) for text in texts]
    kept_sources = [source for source in sources if ADDRESS.sub(checked, source) == source]
    return kept_texts, kept_sources, list(dict.fromkeys(taken))


def _returned(conversation: list[dict[str, Any]]) -> set[str]:
    return {
        _key(address)
        for message in conversation
        if message.get("role") == "tool"
        for address in ADDRESS.findall(str(message.get("content") or ""))
    }


def _clean(address: str) -> str:
    return address.rstrip(TRAILING)


def _key(address: str) -> str:
    """One page, however it was written: no scheme case, trailing slash or punctuation."""
    cleaned = _clean(address).rstrip("/")
    scheme, _, rest = cleaned.partition("://")
    if scheme.lower() == "local":
        # A file is cited with or without the line it was found on.
        rest = rest.split("#", 1)[0]
    return f"{scheme.lower()}://{rest}"
