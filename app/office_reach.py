"""What the office can reach, and how to fix what it cannot.

After Agent Reach's doctor: each source the researcher reads, checked live from
the fetch service that reads it, with the method in use and a fix for what is
down; and, from here, the search provider and the tool servers. Kept for ten
minutes unless the owner asks again, since a check calls out to each platform.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import McpServer
from app.mcp.store import REACHABLE
from app.tools.fetch import FetchError
from app.tools.fetch_client import ExchangeFetcher

CACHE_SECONDS = 600
CACHE: dict[str, Any] = {}


def _search(settings: Settings) -> dict[str, Any]:
    provider = settings.search_provider
    row = {"id": "search", "name": "Web search", "what": "Finding pages to read", "host": None}
    if provider == "static":
        return row | {
            "state": "off",
            "via": "",
            "detail": "No search provider: the researcher reads only pages it is given.",
            "fix": "Set SEARCH_PROVIDER (brave, exa or tavily) and SEARCH_API_KEY in .env.",
        }
    return row | {"state": "ok", "via": provider, "detail": f"Searches with {provider}.", "fix": ""}


def _tools(session: Session) -> dict[str, Any]:
    servers = list(session.scalars(select(McpServer).order_by(McpServer.name)))
    reachable = [server.name for server in servers if server.status == REACHABLE]
    row = {
        "id": "tools",
        "name": "Tool servers",
        "what": "Signed-in platforms, through servers you add and grant",
        "host": None,
    }
    if not servers:
        return row | {
            "state": "off",
            "via": "",
            "detail": "None added.",
            "fix": "Add one under Tools: platforms that need your account are reached this way, "
            "never with your browser's cookies.",
        }
    state = "ok" if reachable else "warn"
    detail = f"{len(reachable)} of {len(servers)} reachable" + (
        f": {', '.join(reachable[:5])}." if reachable else "."
    )
    return row | {
        "state": state,
        "via": "MCP",
        "detail": detail,
        "fix": "" if reachable else "Open Tools to sign in or check each server.",
    }


async def reach(
    session: Session, settings: Settings, fresh: bool = False, now: float | None = None
) -> dict[str, Any]:
    now = now if now is not None else time.monotonic()
    held = CACHE.get("rows")
    if not fresh and held is not None and now - float(CACHE.get("at", 0)) < CACHE_SECONDS:
        rows = held
    elif settings.fetch_exchange is None:
        rows = [
            {
                "id": "fetcher",
                "name": "Reading pages",
                "what": "The fetch service that reads every page",
                "host": None,
                "state": "off",
                "via": "",
                "detail": "The fetch service is not set up for this office.",
                "fix": "Start the office with python3 -m scripts.launch, which starts it.",
            }
        ]
    else:
        try:
            rows = await ExchangeFetcher(settings.fetch_exchange).probe()
        except FetchError as failed:
            rows = [
                {
                    "id": "fetcher",
                    "name": "Reading pages",
                    "what": "The fetch service that reads every page",
                    "host": None,
                    "state": "off",
                    "via": "",
                    "detail": f"The fetch service did not answer ({failed}).",
                    "fix": "Restart the office (python3 -m scripts.upgrade), then check again.",
                }
            ]
        CACHE.update(rows=rows, at=now)
    checked = float(CACHE.get("at", now))
    return {
        "sources": [_search(settings), *rows, _tools(session)],
        "checked_seconds_ago": round(now - checked),
    }
