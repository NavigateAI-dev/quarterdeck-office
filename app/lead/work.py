"""The work tools a lead task gets: the researcher's own, behind the same checks.

Web search and page reading are the researcher's search tool and fetcher, as
the worker guarded them for this run's policies; MCP tools are the ones the
run's project was granted. Results are not cut here: the lead spills them.
"""

from __future__ import annotations

from typing import Any

from app.channels.lookup import lookup_specs
from app.mcp.specs import mcp_specs
from app.orchestrator.graph import AgentSet
from app.tools.pipeline import ToolSpec

SEARCH_RESULTS = 6
REDIRECTS = 2


def lead_work(agents: AgentSet) -> list[ToolSpec]:
    researcher: Any = agents.researcher
    searcher: Any = getattr(researcher, "search", None)
    fetcher: Any = getattr(researcher, "fetcher", None)
    tools: Any = getattr(researcher, "mcp", None)

    async def search(query: str) -> list[Any]:
        return list(await searcher.search(query, limit=SEARCH_RESULTS))

    async def read(address: str) -> Any:
        return await fetcher.fetch(address, follow_redirects=REDIRECTS)

    work = [
        spec
        for spec in lookup_specs(
            search if searcher is not None else None,
            read if fetcher is not None else None,
            None,
            cut=None,
        )
        if spec.name != "look"
    ]
    if tools is not None:
        work += mcp_specs(tools, tools.offered())
    return work
