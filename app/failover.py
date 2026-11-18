"""A backup the owner chose, used when a provider fails.

A provider that runs out of credit, refuses its key, or fails three calls in a
row is set aside, and calls that would have gone to it go to the backup the
owner chose for it in Settings → Models. Nothing else moves a call: a task's
own budget, a model's refusal or a malformed answer are not a provider failing.
The owner chooses every backup; with none, a failure is a failure, as before.

The wrapper sits innermost, inside the budget and policy checks, so a call
that moves is counted and checked exactly as the first one was. Which
providers are set aside is kept for the whole office, not per task: one
task's discovery spares the next. The primary is tried again after five
minutes when the owner asked for work to come back to it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.gateway.client import GatewayError, asked

# The office's model gateway, as a provider with a backup like any other.
GATEWAY = "gateway"

NO_CREDIT = frozenset({"insufficient_balance", "quota_exhausted"})
KEY_REFUSED = frozenset({"credential_invalid", "permission_denied", "credential_unavailable"})
FAILING = frozenset(
    {
        "provider_unavailable",
        "provider_unreachable",
        "gateway_unreachable",
        "provider_timeout",
        "rate_limited",
    }
)
FAILS_IN_A_ROW = 3
RETRY_SECONDS = 300.0


def reason_for(code: str) -> str | None:
    """Why a provider would be set aside for this error, or None if it would not."""
    base = str(code).split(":", 1)[0]
    if base in NO_CREDIT:
        return "no_credit"
    if base in KEY_REFUSED:
        return "key_refused"
    if base in FAILING:
        return "failing"
    return None


class Breaker:
    """Which providers are set aside, why, and since when."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.state: dict[str, dict[str, Any]] = {}

    def view(self, key: str) -> dict[str, Any] | None:
        """The provider's state while it is set aside, else None."""
        entry = self.state.get(key)
        if not entry or entry.get("since") is None:
            return None
        return {"reason": entry["reason"], "since": entry["since"], "at": entry.get("at")}

    def try_primary(self, key: str, come_back: bool) -> bool:
        entry = self.state.get(key) or {}
        since = entry.get("since")
        if since is None:
            return True
        return come_back and self.clock() - since >= RETRY_SECONDS

    def failed(self, key: str, reason: str) -> bool:
        """Count a failure; True when the provider is (still) set aside."""
        entry = self.state.setdefault(key, {"fails": 0, "since": None, "reason": None})
        entry["fails"] += 1
        if reason in ("no_credit", "key_refused") or entry["fails"] >= FAILS_IN_A_ROW:
            entry["since"] = self.clock()
            entry["reason"] = reason
            # Wall-clock time, for the owner: "set aside since 10:44".
            entry["at"] = time.time()
            return True
        return entry["since"] is not None

    def succeeded(self, key: str) -> None:
        self.state.pop(key, None)


BREAKER = Breaker()


class FailoverCompleter:
    """A provider's model, with the backup the owner chose behind it."""

    def __init__(
        self,
        primary: Any,
        key: str,
        backup_key: str,
        make_backup: Callable[[], Any],
        backup_alias: str | None = None,
        come_back: bool = True,
        on_move: Callable[[str], None] | None = None,
        breaker: Breaker = BREAKER,
    ) -> None:
        self.inner = primary
        self.key = key
        self.backup_key = backup_key
        self.make_backup = make_backup
        self.backup_alias = backup_alias
        self.come_back = come_back
        self.on_move = on_move
        self.breaker = breaker
        self._backup: Any = None
        self._told = False

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
        if self.breaker.try_primary(self.key, self.come_back):
            try:
                answer = await _ask(self.inner, model_alias, messages, max_tokens, schema, tools)
            except GatewayError as error:
                reason = reason_for(error.code)
                if reason is None or not self.breaker.failed(self.key, reason):
                    raise
            else:
                self.breaker.succeeded(self.key)
                return answer
        state = self.breaker.view(self.key) or {}
        if not self._told and self.on_move is not None:
            # Said once per task: the owner reads why, not every call that moved.
            self.on_move(str(state.get("reason") or "failing"))
            self._told = True
        if self._backup is None:
            self._backup = self.make_backup()
        return await _ask(
            self._backup, self.backup_alias or model_alias, messages, max_tokens, schema, tools
        )


async def _ask(
    completer: Any,
    model_alias: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    schema: Any,
    tools: list[dict[str, Any]] | None = None,
) -> Any:
    return await completer.complete(
        model_alias, messages, **asked(max_tokens=max_tokens, schema=schema, tools=tools)
    )
