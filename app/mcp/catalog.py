"""A short, reviewed catalog of tool servers, shipped with the office.

The owner asked for servers from a collection such as mcpservers.org. That site
refuses automated reading; the official MCP Registry is open, but it is also
noisy — a search for "exa" returns Texas cost calculators — and full of relays
of other people's services. So the catalog is a list a person reviewed:
`catalog.json`, beside this file.

Each entry was checked on the date the file records: an unauthenticated MCP
`initialize` against its address (does it answer, and how does it ask to sign
in?) and the vendor's own documentation for any key it accepts. Only Streamable
HTTP servers are listed, because that is all the office speaks. A server that
signs in with an account (OAuth) is added first and then signed in to from its
card: the vendor's own page, in the owner's browser.

Adding from the catalog is adding by hand with the form filled in: the owner's
review of each tool applies unchanged, and nothing here is ever installed by
the office on its own. `python3 -m scripts.mcp_catalog` re-checks the entries
for a person to review.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import McpServer

CATALOG = Path(__file__).with_name("catalog.json")

# In the order the dialog shows them.
CATEGORIES = {
    "research": "Research and search",
    "knowledge": "Documentation and knowledge",
    "projects": "Projects and work tracking",
    "customers": "Customers and support",
    "analytics": "Analytics and market data",
    "content": "Design, content and files",
    "communication": "Meetings and communication",
    "finance": "Payments and finance",
    "data": "Web data",
}
SIGN_IN = {
    "none": "No sign-in",
    "key": "API key",
    "account": "Sign in with your account",
}


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=128)
    vendor: str = Field(min_length=1)
    # The vendor's own domain; a first-party address lives on it.
    vendor_domain: str = Field(min_length=3)
    url: str = Field(pattern=r"^https://")
    category: Literal[
        "research",
        "knowledge",
        "projects",
        "customers",
        "analytics",
        "content",
        "communication",
        "finance",
        "data",
    ]
    # What it gives the office, in the office's words.
    what: str = Field(min_length=1)
    sign_in: Literal["none", "key", "account"]
    # Whether the vendor lets the office register itself for sign-in (RFC 7591).
    # One that does not issues each client by hand, which the office cannot ask.
    self_registers: bool = True
    # Where to get a key and what kind, for servers that take one.
    key_help: str = ""
    docs: str = Field(pattern=r"^https://")
    # The official registry entry, where the vendor published one.
    registry: str | None = None
    # A relay serves someone else's service; it must say whose.
    relay: bool = False
    # Anything the owner should know before adding it.
    note: str = ""

    @property
    def addable(self) -> bool:
        """An account server is added, then signed in to, if its vendor lets
        the office register itself; otherwise adding it would only fail."""
        return self.sign_in != "account" or self.self_registers

    @model_validator(mode="after")
    def consistent(self) -> CatalogEntry:
        host = urlparse(self.url).hostname or ""
        first_party = host == self.vendor_domain or host.endswith(f".{self.vendor_domain}")
        if not self.relay and not first_party:
            raise ValueError(f"{self.id}: address is not on {self.vendor_domain}")
        if not self.self_registers and self.sign_in != "account":
            raise ValueError(f"{self.id}: only account sign-in involves registering")
        if self.sign_in == "key" and not self.key_help.strip():
            raise ValueError(f"{self.id}: a key is needed but nothing says where to get one")
        return self


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_on: date
    source: str
    entries: list[CatalogEntry]

    @model_validator(mode="after")
    def unique(self) -> Catalog:
        ids = [entry.id for entry in self.entries]
        urls = [entry.url.rstrip("/") for entry in self.entries]
        if len(set(ids)) != len(ids) or len(set(urls)) != len(urls):
            raise ValueError("catalog ids and addresses must be unique")
        return self


@lru_cache(maxsize=1)
def load() -> Catalog:
    return Catalog.model_validate(json.loads(CATALOG.read_text(encoding="utf-8")))


def listed(session: Session) -> dict[str, Any]:
    """The catalog by category, each entry saying whether it is already added."""
    catalog = load()
    added = {row.rstrip("/") for row in session.scalars(select(McpServer.url))}
    groups = []
    for key, label in CATEGORIES.items():
        entries = [
            {
                **entry.model_dump(),
                "addable": entry.addable,
                "sign_in_label": SIGN_IN[entry.sign_in],
                "added": entry.url.rstrip("/") in added,
            }
            for entry in catalog.entries
            if entry.category == key
        ]
        if entries:
            groups.append({"key": key, "label": label, "entries": entries})
    return {
        "checked_on": catalog.checked_on.isoformat(),
        "source": catalog.source,
        "categories": groups,
    }
