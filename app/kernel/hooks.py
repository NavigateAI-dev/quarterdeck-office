"""Named hook points on the office's work.

Two kinds, as in DeepSeek Harness:
- a waterfall passes a value through its handlers in order; each returns the
  value the next one sees (e.g. `step.before` adding skill text to a prompt);
- a guard may only deny. Guards run in order and the first denial wins, so the
  order plugins load in can never turn a denial into permission. A guard that
  crashes denies.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

logger = logging.getLogger(__name__)

Disposer = Callable[[], None]
_sequence = count()


class Denied(Exception):
    """A guard refused. `reason` is said to the owner."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(order=True)
class _Handler:
    order: int
    sequence: int
    fn: Callable[[Any], Any] = field(compare=False)


async def _settle(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class HookBus:
    def __init__(self) -> None:
        self._waterfalls: dict[str, list[_Handler]] = {}
        self._guards: dict[str, list[_Handler]] = {}

    @staticmethod
    def _add(table: dict[str, list[_Handler]], name: str, fn: Any, order: int) -> Disposer:
        handler = _Handler(order, next(_sequence), fn)
        handlers = table.setdefault(name, [])
        handlers.append(handler)
        handlers.sort()

        def remove() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return remove

    def on(self, name: str, fn: Callable[[Any], Any], order: int = 100) -> Disposer:
        return self._add(self._waterfalls, name, fn, order)

    def guard(self, name: str, fn: Callable[[Any], Any], order: int = 100) -> Disposer:
        """`fn(subject)` returns None to allow, or a reason to deny."""
        return self._add(self._guards, name, fn, order)

    async def waterfall(self, name: str, value: Any) -> Any:
        for handler in list(self._waterfalls.get(name, [])):
            value = await _settle(handler.fn(value))
        return value

    async def check(self, name: str, subject: Any) -> None:
        for handler in list(self._guards.get(name, [])):
            try:
                reason = await _settle(handler.fn(subject))
            except Exception as error:
                logger.exception('{"event":"guard_failed","hook":"%s"}', name)
                raise Denied(f"guard failed: {type(error).__name__}") from error
            if reason:
                raise Denied(str(reason))
