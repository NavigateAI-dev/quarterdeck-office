"""Slack app manifests for agent bots, so creating one needs no YAML editing.

Every bot gets the same least-privilege scopes as the office bot, Socket Mode
(no public URL) and the Messages tab so the owner can DM it.
"""

import json
import re
from typing import Any
from urllib.parse import quote

SCOPES = (
    "chat:write",
    # Lets the office bot post under an agent's name and icon.
    "chat:write.customize",
    "channels:read",
    "groups:read",
    "app_mentions:read",
    "im:history",
    "im:read",
    "im:write",
    "users:read",
    # The work itself in the thread it was asked in: its document as a file.
    "files:write",
    # Pictures and files the owner shares with a question.
    "files:read",
    # A 💡 the owner puts on a message keeps it as an idea: the
    # reaction, and the one message it is on (read only when the owner reacts).
    "reactions:read",
    "channels:history",
    "groups:history",
)
EVENTS = ("app_mention", "message.im", "reaction_added")
APP_NAME_LIMIT = 35  # Slack's limit for app names.
CREATE_URL = "https://api.slack.com/apps?new_app=1&manifest_json="
UNSAFE = re.compile(r"[^\w .()'-]")


def slack_manifest(agent_name: str, role: str) -> dict[str, Any]:
    name = UNSAFE.sub("", agent_name).strip()[:APP_NAME_LIMIT] or "Quarterdeck agent"
    return {
        "display_information": {
            "name": name,
            "description": f"Quarterdeck agent ({role[:40]}). Answers its owner; never acts alone.",
            "background_color": "#1f3d2b",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": name, "always_online": False},
        },
        "oauth_config": {"scopes": {"bot": list(SCOPES)}},
        "settings": {
            "event_subscriptions": {"bot_events": list(EVENTS)},
            # Buttons arrive over Socket Mode, so no request URL is needed.
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def create_app_url(manifest: dict[str, Any]) -> str:
    return CREATE_URL + quote(json.dumps(manifest, separators=(",", ":")))
