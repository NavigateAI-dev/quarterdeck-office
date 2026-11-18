"""Putting the office's policies in front of the work itself.

Each wrapper keeps the tool's own behaviour and adds one question before it
runs: may this happen, for this task, under this office's rules?
"""

from typing import Any
from uuid import UUID

from app.gateway.client import asked
from app.policies.engine import Hook
from app.policies.store import PolicyGate


class GuardedFetcher:
    """A page fetcher that asks the office's policies about each address."""

    def __init__(
        self, inner: Any, gate: PolicyGate, run_id: UUID | None, project_id: UUID | None
    ) -> None:
        self.inner = inner
        self.gate = gate
        self.run_id = run_id
        self.project_id = project_id

    def __getattr__(self, name: str) -> Any:
        # Everything the researcher reads from the fetcher stays as it was.
        return getattr(self.inner, name)

    async def fetch(self, url: str, **kwargs: Any) -> Any:
        self.gate.enforce(
            Hook.TOOL_CALL,
            "web_fetch",
            run_id=self.run_id,
            project_id=self.project_id,
            url=url,
        )
        return await self.inner.fetch(url, **kwargs)

    async def fetch_many(self, urls: list[str], **kwargs: Any) -> Any:
        for url in urls:
            self.gate.enforce(
                Hook.TOOL_CALL,
                "web_fetch",
                run_id=self.run_id,
                project_id=self.project_id,
                url=url,
            )
        return await self.inner.fetch_many(urls, **kwargs)


class GuardedSearch:
    """A search tool that asks the office's policies about each query."""

    def __init__(
        self, inner: Any, gate: PolicyGate, run_id: UUID | None, project_id: UUID | None
    ) -> None:
        self.inner = inner
        self.gate = gate
        self.run_id = run_id
        self.project_id = project_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def search(self, query: str, *, limit: int = 10) -> Any:
        self.gate.enforce(
            Hook.TOOL_CALL,
            "web_search",
            run_id=self.run_id,
            project_id=self.project_id,
            query=query[:200],
        )
        return await self.inner.search(query, limit=limit)


class GuardedCompleter:
    """A model that asks the office's policies before it is called."""

    def __init__(
        self,
        inner: Any,
        gate: PolicyGate,
        run_id: UUID | None,
        project_id: UUID | None,
        role: str,
    ) -> None:
        self.inner = inner
        self.gate = gate
        self.run_id = run_id
        self.project_id = project_id
        self.role = role

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: Any = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        self.gate.enforce(
            Hook.MODEL_CALL,
            self.role,
            run_id=self.run_id,
            project_id=self.project_id,
            model=model_alias,
        )
        return await self.inner.complete(
            model_alias, messages, **asked(max_tokens=max_tokens, schema=schema, tools=tools)
        )
