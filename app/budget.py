"""Run budgets.

Every model call reserves its worst case before it is made: the prompt's
estimated tokens plus the maximum output. A reservation that would pass the
token budget, a cost already at the cost budget, or execution past the time
budget refuses the call as `budget_exhausted`. Afterwards actual usage
replaces the reservation. Usage a provider does not report is counted at the
reservation, never as zero, and cost is then marked as not fully reported.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.gateway.client import Completion, GatewayError, asked
from app.gateway.direct import Completer

# What a piece of work started from a discussion may spend before it asks.
HANDOFF_COST = 0.50

CHARS_PER_TOKEN = 4


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tokens: int = Field(gt=0, le=50_000_000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    # Active execution per worker job; time waiting for a person is not counted.
    max_minutes: int | None = Field(default=None, gt=0, le=24 * 60)


class BudgetExhausted(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"budget_exhausted:{kind}")


def estimate_prompt_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> int:
    """Characters over four: every turn's content, its tool calls, and the tool catalogue."""
    characters = sum(_characters(message) for message in messages)
    if tools:
        characters += len(json.dumps(tools))
    return characters // CHARS_PER_TOKEN + 1


def _characters(message: dict[str, Any]) -> int:
    # A list of parts counts one per part, as it always has; tool calls count in full.
    content = message.get("content") or ""
    calls = message.get("tool_calls")
    return len(content) + (len(json.dumps(calls)) if calls else 0)


@dataclass
class RunLedger:
    budget: Budget
    max_output_tokens: int
    spent_tokens: int = 0
    spent_cost: float = 0.0
    cost_reported: bool = True
    reserved_tokens: int = 0
    exhausted: str | None = None
    clock: Callable[[], float] = time.monotonic
    started: float = field(default=0.0)
    # The task's limits as its record holds them now. A limit the owner raised
    # while the task runs is read before the old one refuses a call.
    refresh: Callable[[], Budget | None] | None = None

    def __post_init__(self) -> None:
        self.started = self.clock()

    def _refuse(self, kind: str) -> None:
        self.exhausted = kind
        raise BudgetExhausted(kind)

    def reserve(
        self,
        messages: list[dict[str, Any]],
        max_output: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        try:
            return self._reserve(messages, max_output, tools)
        except BudgetExhausted:
            newer = self.refresh() if self.refresh is not None else None
            if newer is None or newer == self.budget:
                raise
            self.budget = newer
            self.exhausted = None
            return self._reserve(messages, max_output, tools)

    def _reserve(
        self,
        messages: list[dict[str, Any]],
        max_output: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        if self.budget.max_minutes is not None:
            if self.clock() - self.started >= self.budget.max_minutes * 60:
                self._refuse("time")
        if self.budget.max_cost_usd is not None and self.spent_cost >= self.budget.max_cost_usd:
            self._refuse("cost")
        amount = estimate_prompt_tokens(messages, tools) + (max_output or self.max_output_tokens)
        if self.spent_tokens + self.reserved_tokens + amount > self.budget.max_tokens:
            self._refuse("tokens")
        self.reserved_tokens += amount
        return amount

    def release(self, amount: int) -> None:
        self.reserved_tokens -= amount

    def charge_unreported(self, amount: int) -> None:
        """A call the provider billed without usage we can see: count it all."""
        self.release(amount)
        self.spent_tokens += amount
        self.cost_reported = False

    def commit(self, amount: int, completion: Completion) -> None:
        self.release(amount)
        prompt, output = completion.usage.prompt_tokens, completion.usage.completion_tokens
        self.spent_tokens += amount if prompt is None or output is None else prompt + output
        if completion.estimated_cost is None:
            self.cost_reported = False
        else:
            self.spent_cost += completion.estimated_cost


def room_to_finish(
    budget: dict[str, Any], spent: dict[str, Any], material: str, max_output: int
) -> dict[str, Any]:
    """The task's limits with room for one last writing pass.

    Finishing a task writes its answer from what it found; a task stopped at
    its ceiling would be refused that one call. Room for it, and one retry, is
    added to the limit that was reached and to nothing else.
    """
    need = 2 * (len(material) // CHARS_PER_TOKEN + 4_000 + max_output)
    raised = dict(budget)
    tokens = int(spent.get("tokens") or 0)
    if "max_tokens" in raised and tokens + need > int(raised["max_tokens"]):
        raised["max_tokens"] = min(tokens + need, 50_000_000)
    limit = raised.get("max_cost_usd")
    cost = spent.get("cost_usd")
    if limit is not None and cost is not None and float(cost) >= float(limit) * 0.9:
        raised["max_cost_usd"] = round(float(cost) + max(0.25, float(limit) * 0.25), 2)
    return raised


class BudgetedCompleter:
    """Wraps a role's completer so no call can pass the run's budget."""

    def __init__(self, inner: Completer, ledger: RunLedger) -> None:
        self.inner = inner
        self.ledger = ledger

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: Any = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        try:
            amount = self.ledger.reserve(messages, max_tokens, tools)
        except BudgetExhausted as exhausted:
            # The kind travels with the code: which ceiling was reached decides
            # what the owner can usefully do about it.
            raise GatewayError(f"budget_exhausted:{exhausted.kind}") from None
        try:
            completion = await self.inner.complete(
                model_alias, messages, **asked(max_tokens=max_tokens, schema=schema, tools=tools)
            )
        except GatewayError as error:
            # A response cut off at the output cap was generated and billed.
            if error.code == "incomplete_response":
                self.ledger.charge_unreported(amount)
            else:
                self.ledger.release(amount)
            raise
        except BaseException:
            self.ledger.release(amount)
            raise
        self.ledger.commit(amount, completion)
        return completion


def budget_from(payload: dict[str, Any] | None, default: Budget) -> Budget:
    return Budget.model_validate(payload) if payload else default
