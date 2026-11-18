"""A named registry whose every registration can be undone."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, item: T) -> Callable[[], None]:
        if name in self._items:
            raise ValueError(f"{self.kind} {name!r} is already registered")
        self._items[name] = item

        def remove() -> None:
            if self._items.get(name) is item:
                del self._items[name]

        return remove

    def get(self, name: str) -> T | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return list(self._items)

    def items(self) -> list[tuple[str, T]]:
        return list(self._items.items())
