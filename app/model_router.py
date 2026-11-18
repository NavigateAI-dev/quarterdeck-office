"""Which models a task runs on: no stronger than it needs.

Learned from LangChain's model router in Open SWE (`openswe/middleware/
model_selection.py`, Oct 2026): past a point a stronger model adds little
quality and much cost, so each task is classified once, from its request, into
a tier. There, routing cut median cost per thread by 64% with no measurable loss
in merged work; always using the fast model was stopped within a day for poor
output. So here a tier is a **ceiling**, never a floor:

- `performance` keeps the office's own setup: every role on the model it has.
- `balanced` and `fast` name a target model, taken from the owner's tested
  connections with a published price (the cheapest is `fast`, the middle one
  `balanced`). A role moves to it only when its own model costs at least
  `SAVING` times as much; a role already on a cheap model stays where it is, and
  a role whose price is unknown is left alone.
- **Jev chooses**, from the request and a plain description of each tier, and is
  believed only when it is confident (≥ 0.6) and quick (3 s). Anything else
  keeps the office's setup.
- **One decision per task**, recorded with the roles it moved, so a resumed
  task keeps its models and the provider's prompt cache stays warm.
- A model the owner pinned to a role in a project, or an agent's own
  connection, is never moved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.model_catalog import describe

Tier = Literal["fast", "balanced", "performance"]
Outcome = Literal["routed", "low_confidence", "no_choice", "unavailable", "nothing_to_route"]

MIN_CONFIDENCE = 0.6
TIMEOUT_SECONDS = 3.0
# A move is worth a provider change only when it at least halves the price.
SAVING = 2.0
# The ask, not the material: the end of a long request is where the ask is.
REQUEST_CHARS = 8_000
USABLE = ("MODEL_AVAILABLE", "VERIFIED")
ENTRY_KEYS = ("connection_id", "provider", "model_id", "base_url")

INSTRUCTIONS = (
    "Choose one fixed model profile for this office task. Use the least expensive "
    "profile likely to complete the whole task well.\n\n"
    "Clear scope, one obvious answer, material supplied with the request, and outcomes "
    "that are easy to check or undo lower the capability needed. Ambiguous or conflicting "
    "requirements, conflicting sources, judgment with money, legal, compliance or "
    "reputation at stake, broad scope and real tradeoffs raise it. The length of the "
    "request and how long the work will take are not difficulty signals."
)

CRITERIA: dict[Tier, str] = {
    "fast": (
        "Use for direct lookups and short factual answers, status checks, extracting, "
        "reformatting or translating given material, short summaries of supplied text, "
        "and routine replies with an obvious answer. The least capable and least "
        "expensive profile."
    ),
    "balanced": (
        "Use for ordinary research across a few sources, comparisons, bounded analyses, "
        "drafting a document, plan or message from clear requirements, and summaries that "
        "need judgment. Mid-tier capability and cost."
    ),
    "performance": (
        "Use for strategy and high-stakes decisions, ambiguous or conflicting requirements, "
        "legal, financial or compliance judgment, conflicting evidence, and novel reasoning "
        "across many sources that the owner will act on. The most capable and most "
        "expensive profile."
    ),
}


class RouteJudge(Protocol):
    async def model_route(
        self, task: str, instructions: str, criteria: dict[str, str]
    ) -> tuple[str, float]: ...


@dataclass(frozen=True)
class Target:
    """A tier's model: an owner-chosen role model entry, and its price per million."""

    entry: dict[str, Any]
    price: float


@dataclass(frozen=True)
class RouteDecision:
    tier: Tier | None
    confidence: float | None
    outcome: Outcome
    # The tier's model; None keeps every role's own (performance, or no route).
    target: Target | None = None
    # role -> the entry it moved to, once decided.
    moved: dict[str, dict[str, Any]] = field(default_factory=dict)


def price_of(catalogue: dict[str, Any], provider: str, model_id: str) -> float | None:
    """Input plus output price per million tokens; None for local or unpriced models."""
    about = describe(catalogue, provider, model_id)
    price_in, price_out = about.get("input_per_million"), about.get("output_per_million")
    if about.get("local") or price_in is None or price_out is None:
        return None
    return float(price_in) + float(price_out)


def targets(connections: list[dict[str, Any]], catalogue: dict[str, Any]) -> dict[Tier, Target]:
    """The fast and balanced models: the owner's tested, priced connections, cheapest first."""
    priced = []
    for row in connections:
        if row.get("status") not in USABLE:
            continue
        price = price_of(catalogue, str(row["provider"]), str(row["model_id"]))
        if price is not None:
            priced.append((price, str(row["connection_id"]), row))
    if not priced:
        return {}
    ranked = sorted(priced, key=lambda item: (item[0], item[1]))
    chosen: dict[Tier, Target] = {"fast": _target(ranked[0])}
    if len(ranked) >= 2:
        chosen["balanced"] = _target(ranked[len(ranked) // 2])
    return chosen


def _target(item: tuple[float, str, dict[str, Any]]) -> Target:
    return Target({key: item[2].get(key) for key in ENTRY_KEYS}, item[0])


async def decide(
    request: str,
    pool: Mapping[Tier, Target],
    judge: RouteJudge | None,
    timeout_s: float = TIMEOUT_SECONDS,
) -> RouteDecision:
    """Jev's tier for this request, or no route when it cannot say with confidence."""
    if judge is None:
        return RouteDecision(None, None, "unavailable")
    if not pool:
        return RouteDecision(None, None, "nothing_to_route")
    offered: dict[str, str] = {
        tier: CRITERIA[tier] for tier in CRITERIA if tier in pool or tier == "performance"
    }
    try:
        async with asyncio.timeout(timeout_s):
            choice, confidence = await judge.model_route(
                request[-REQUEST_CHARS:], INSTRUCTIONS, offered
            )
    except Exception:  # noqa: BLE001 - routing is optional; the office's setup stands
        return RouteDecision(None, None, "unavailable")
    if not MIN_CONFIDENCE <= confidence <= 1.0:
        return RouteDecision(None, confidence, "low_confidence")
    if choice == "performance":
        return RouteDecision("performance", confidence, "routed")
    tier = next((name for name in pool if name == choice), None)
    if tier is None:
        return RouteDecision(None, confidence, "no_choice")
    return RouteDecision(tier, confidence, "routed", pool[tier])


def moves(
    decision: RouteDecision, current: Mapping[str, float | None], pinned: set[str]
) -> dict[str, dict[str, Any]]:
    """The roles to move to the tier's model: those whose own model costs `SAVING`× more."""
    target = decision.target
    if target is None:
        return {}
    return {
        role: dict(target.entry)
        for role, price in current.items()
        if role not in pinned and price is not None and price >= SAVING * target.price
    }
