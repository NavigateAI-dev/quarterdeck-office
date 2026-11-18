"""Scopes: who owns a registration, and when it ends.

Every scope sits in one tree: root (the process) → office → run → agent, with a
plugin scope under whatever loaded it. Anything registered through a scope is
undone when the scope is disposed, children first, then its own cleanups in
reverse order. A failing cleanup is logged and never stops the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from app.kernel.registry import Registry

logger = logging.getLogger(__name__)

Disposer = Callable[[], None]
T = TypeVar("T")


class Scope:
    def __init__(self, kind: str, id: str = "", parent: Scope | None = None) -> None:
        self.kind = kind
        self.id = id
        self.parent = parent
        self.children: list[Scope] = []
        self.disposed = False
        self._cleanups: list[Disposer] = []
        self._services: dict[str, Any] = {}

    def __repr__(self) -> str:
        return f"Scope({self.path()})"

    def path(self) -> str:
        own = f"{self.kind}:{self.id}" if self.id else self.kind
        return f"{self.parent.path()}/{own}" if self.parent else own

    def _alive(self) -> None:
        if self.disposed:
            raise RuntimeError(f"{self.path()} is disposed")

    def child(self, kind: str, id: str) -> Scope:
        self._alive()
        scope = Scope(kind, id, self)
        self.children.append(scope)
        return scope

    def defer(self, cleanup: Disposer) -> None:
        """Run `cleanup` when this scope ends."""
        self._alive()
        self._cleanups.append(cleanup)

    def provide(self, name: str, service: Any) -> Disposer:
        """Offer a service to this scope and everything under it."""
        self._alive()
        if name in self._services:
            raise ValueError(f"service {name!r} is already provided in {self.path()}")
        self._services[name] = service

        def remove() -> None:
            self._services.pop(name, None)

        self.defer(remove)
        return remove

    def export(self, name: str, service: Any) -> Disposer:
        """Offer a service to the parent scope, for as long as this scope lives.

        How a plugin gives the office something: the office sees it, and it
        goes away when the plugin is disposed.
        """
        self._alive()
        if self.parent is None:
            raise RuntimeError(f"{self.path()} has no parent to export {name!r} to")
        remove = self.parent.provide(name, service)
        self.defer(remove)
        return remove

    def get(self, name: str) -> Any:
        """The nearest service by this name, looking up the tree."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope._services:
                return scope._services[name]
            scope = scope.parent
        raise LookupError(f"no service {name!r} visible from {self.path()}")

    def has(self, name: str) -> bool:
        try:
            self.get(name)
        except LookupError:
            return False
        return True

    def register(self, registry: Registry[T], name: str, item: T) -> Disposer:
        """Register into a registry for as long as this scope lives."""
        self._alive()
        remove = registry.register(name, item)
        self.defer(remove)
        return remove

    def dispose(self) -> None:
        if self.disposed:
            return
        for child in list(reversed(self.children)):
            child.dispose()
        self.disposed = True
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                cleanup()
            except Exception:
                logger.exception('{"event":"scope_cleanup_failed","scope":"%s"}', self.path())
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)
