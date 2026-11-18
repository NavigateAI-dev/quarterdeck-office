"""The office's built-in plugins, loaded into the office scope at startup."""

from app.kernel import Plugin
from app.plugins.notifications import NotificationsPlugin

BUILTIN_PLUGINS: list[Plugin] = [NotificationsPlugin()]
