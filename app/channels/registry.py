"""Which chat platforms exist, and which are usable now."""

from typing import Any

from app.channels.base import ChannelAdapter, ChannelError
from app.channels.slack import SlackAdapter

KINDS: dict[str, dict[str, Any]] = {
    "slack": {"name": "Slack", "status": "available", "adapter": SlackAdapter},
    "teams": {"name": "Microsoft Teams", "status": "planned", "adapter": None},
    "discord": {"name": "Discord", "status": "planned", "adapter": None},
    "lark": {"name": "Lark / Feishu", "status": "planned", "adapter": None},
}


def available_kinds() -> list[dict[str, str]]:
    return [{"kind": k, "name": v["name"], "status": v["status"]} for k, v in KINDS.items()]


def adapter_for(kind: str) -> ChannelAdapter:
    entry = KINDS.get(kind)
    if entry is None or entry["adapter"] is None:
        raise ChannelError("channel_not_available")
    adapter: ChannelAdapter = entry["adapter"]()
    return adapter
