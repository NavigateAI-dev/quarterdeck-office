"""Loading the office's plugins (built-in plugins only).

Each plugin gets its own scope under the scope that loads it, so everything it
registers goes away with it. Requirements are checked before any plugin
starts; a plugin that fails to start takes the whole load down with a message
naming it, rather than leaving a half-started office.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.kernel.scope import Scope


class Plugin(Protocol):
    name: str
    # Services or earlier plugins this one needs.
    requires: tuple[str, ...]

    def setup(self, scope: Scope) -> None: ...


class PluginError(RuntimeError):
    pass


def load_plugins(scope: Scope, plugins: Sequence[Plugin]) -> dict[str, Scope]:
    names: set[str] = set()
    for plugin in plugins:
        if plugin.name in names:
            raise PluginError(f"plugin {plugin.name!r} is listed twice")
        missing = [need for need in plugin.requires if need not in names and not scope.has(need)]
        if missing:
            raise PluginError(f"plugin {plugin.name!r} needs {', '.join(missing)}")
        names.add(plugin.name)

    loaded: dict[str, Scope] = {}
    for plugin in plugins:
        own = scope.child("plugin", plugin.name)
        try:
            plugin.setup(own)
        except Exception as error:
            own.dispose()
            for started in reversed(loaded.values()):
                started.dispose()
            raise PluginError(f"plugin {plugin.name!r} failed to start: {error}") from error
        loaded[plugin.name] = own
    return loaded
