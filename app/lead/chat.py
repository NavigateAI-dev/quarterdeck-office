"""The lead in conversations.

A conversation spends from the bot's daily chat allowance, not a task's
budget. Every model call the lead makes — its own, a delegated brief's, the
critic's — is checked against what is left before it is made and recorded
after, as a quick answer's calls always were. A call that would pass the
allowance is not made: the loop ends with its best answer so far.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.budget import estimate_prompt_tokens
from app.channels.conversation import used_tokens
from app.channels.lookup import (
    MAX_PICTURES,
    ChatAnswer,
    look_with_browser,
    lookup_specs,
    watch_spec,
)
from app.config import Settings
from app.gateway.client import Completion, GatewayError, asked
from app.lead.loop import CUT_SHORT
from app.lead.prompt import Section
from app.lead.service import LeadRequest, answer
from app.office_models import can_see
from app.office_settings import effective
from app.tools.deepseek import with_deepseek_fallback
from app.tools.fetch_client import ExchangeFetcher
from app.tools.web import StaticSearchTool, build_search_tool

CHAT_LIMIT = "budget_exhausted:chat"


class AllowanceCompleter:
    def __init__(
        self,
        inner: Any,
        *,
        cap: int,
        allowance: int,
        used_today: Callable[[], int],
        record: Callable[[int, float | None], None],
    ) -> None:
        self.inner = inner
        self.cap = cap
        self.allowance = allowance
        self.used_today = used_today
        self.record = record

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: Any = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        cap = min(max_tokens or self.cap, self.cap)
        reserve = estimate_prompt_tokens(messages, tools) + cap
        if self.used_today() + reserve > self.allowance:
            raise GatewayError(CHAT_LIMIT)
        completion: Completion = await self.inner.complete(
            model_alias, messages, **asked(max_tokens=cap, schema=schema, tools=tools)
        )
        usage = completion.usage
        self.record(
            used_tokens(usage.prompt_tokens, usage.completion_tokens, reserve),
            completion.estimated_cost,
        )
        return completion


SEARCH_RESULTS = 6
FACTS_CHARS = 12_000
USED_UP = (
    "Today's chat allowance for this bot is used up; it resets at midnight UTC. To go on now, "
    "raise Chat tokens per bot per day in the office's Settings."
)


@dataclass
class ChatAsk:
    """One message in a conversation, and what answering it may use."""

    text: str
    history: list[dict[str, Any]]
    persona: str
    facts: dict[str, Any]
    completer: Any  # the agent's own connection, or the office's model for its role
    alias: str
    settings: Settings
    lookup: tuple[Any, Any, Any]  # search, read, look — each None where not running
    used_today: Callable[[], int]
    record: Callable[[int, float | None], None]
    allowance: int
    # Tools for this speaker alone: what only the owner may do.
    own_work: list[Any] = field(default_factory=list)


async def chat_reply(ask: ChatAsk) -> ChatAnswer | str | None:
    """The lead answers a conversation: looks things up, offers a task or a
    watch, shares what the browser saw, all within the chat allowance."""
    pictures: list[bytes] = []
    watches: list[dict[str, Any]] = []
    search, read, look = ask.lookup

    async def looked(address: str) -> tuple[str, bytes | None]:
        shown, picture = await look(address)
        if picture:
            pictures.append(picture)
        return shown, picture

    work = [
        *lookup_specs(search, read, looked if look is not None else None, cut=None),
        watch_spec(watches),
        *ask.own_work,
    ]
    model = AllowanceCompleter(
        ask.completer,
        cap=ask.settings.chat_max_output_tokens,
        allowance=ask.allowance,
        used_today=ask.used_today,
        record=ask.record,
    )
    reply = await answer(
        LeadRequest(
            request=ask.text,
            completer_for=lambda role: (model, ask.alias),
            work=work,
            now=datetime.now(UTC),
            doctrine=ask.persona,
            sections=[Section("Office facts", json.dumps(ask.facts, default=str), FACTS_CHARS)],
            history=ask.history,
            sees=await can_see(ask.completer, ask.alias),
        )
    )
    if reply.stopped == "error":
        return None
    if reply.reason == CHAT_LIMIT and (not reply.text or reply.text.startswith(CUT_SHORT)):
        return USED_UP
    return ChatAnswer(
        text=reply.text,
        files=[(f"looked-at-{n + 1}.jpg", p) for n, p in enumerate(pictures)][:MAX_PICTURES],
        task=reply.proposals[0]["objective"] if reply.proposals else None,
        watch=watches[0] if watches else None,
    )


def office_lookup(
    settings: Settings, session_factory: sessionmaker[Session]
) -> tuple[Any, Any, Any]:
    """Search, read and look for a conversation, each only where the office runs it.

    Shared by Slack and the office's own discussions. Pages are read by the
    office's fetch service (with DeepSeek behind it when turned on); the browser
    only when the owner's Browsing setting is on.
    """
    tool = build_search_tool(settings)

    async def search(query: str) -> list[Any]:
        return list(await tool.search(query, limit=SEARCH_RESULTS))

    searcher = None if isinstance(tool, StaticSearchTool) else search
    reader = None
    if settings.fetch_exchange is not None:
        reader = with_deepseek_fallback(ExchangeFetcher(settings.fetch_exchange), settings).fetch
    looker = None
    exchange = settings.browser_exchange
    with session_factory() as session:
        browsing = bool(effective(session, settings)["browser_on"])
    if exchange is not None and not browsing:

        async def switched_off(address: str) -> tuple[str, bytes | None]:
            raise LookupError("browser_switched_off")

        looker = switched_off
    elif exchange is not None:

        async def look(address: str) -> tuple[str, bytes | None]:
            return await look_with_browser(exchange, address)

        looker = look
    return searcher, reader, looker
