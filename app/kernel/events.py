"""Facts the office announces, for whoever cares.

A publisher never imports its listeners: the run engine says a run ended, and
Slack notifications, the Desk and anything a plugin adds react to it. A failing
subscriber is logged and never reaches the publisher or the other subscribers.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Disposer = Callable[[], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[tuple[type, Callable[[Any], Any]]] = []

    def subscribe(self, event_type: type, fn: Callable[[Any], Any]) -> Disposer:
        entry = (event_type, fn)
        self._subscribers.append(entry)

        def remove() -> None:
            if entry in self._subscribers:
                self._subscribers.remove(entry)

        return remove

    async def publish(self, event: Any) -> None:
        for event_type, fn in list(self._subscribers):
            if not isinstance(event, event_type):
                continue
            try:
                result = fn(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    '{"event":"event_subscriber_failed","type":"%s"}', type(event).__name__
                )
