"""The office kernel: scopes, registries, hooks, events and plugins.

Holds no feature logic. Imports nothing else from `app`.
"""

from app.kernel.events import EventBus
from app.kernel.hooks import Denied, HookBus
from app.kernel.plugins import Plugin, PluginError, load_plugins
from app.kernel.registry import Registry
from app.kernel.scope import Disposer, Scope

__all__ = [
    "Denied",
    "Disposer",
    "EventBus",
    "HookBus",
    "Plugin",
    "PluginError",
    "Registry",
    "Scope",
    "load_plugins",
]
