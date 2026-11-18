"""The private remote route, as the office sees it.

The remote container runs Tailscale beside the office and answers through an
exchange folder: a status file it rewrites every few seconds, and one file per
request. The office never runs Tailscale itself, never sees the Docker socket,
and keeps nothing secret: an auth key is handed over once and not stored.

What the owner sees is one of the plain stages in STAGES, each meaning one
thing; "confirmed from phone" only once a phone has signed in through it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.db.models import AccessPoint

STATUS_FILE = "remote.status.json"
# Older than this, the container is taken to be stopped.
STALE_SECONDS = 30.0
WAITING = ("NeedsLogin", "NeedsMachineAuth", "NoState", "Starting")

STAGES = {
    "setup_required": "Setup required",
    "awaiting_authorization": "Awaiting authorization",
    "off": "Off",
    "checking": "Checking",
    "ready_for_phone_test": "Ready for phone test",
    "confirmed_from_phone": "Confirmed from phone",
    "connection_problem": "Connection problem",
}


class RemoteControl:
    """Requests to the remote container, through its exchange folder."""

    def __init__(self, exchange: Path, timeout_seconds: float = 90.0, poll: float = 0.2) -> None:
        self.exchange = exchange
        self.timeout_seconds = timeout_seconds
        self.poll = poll

    def seen(self) -> dict[str, Any] | None:
        """What the container last wrote, or None when it is not running."""
        try:
            data = json.loads((self.exchange / STATUS_FILE).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or time.time() - float(data.get("at") or 0) > STALE_SECONDS:
            return None
        return data

    async def ask(self, action: str, **body: Any) -> dict[str, Any]:
        job_id = uuid4().hex
        temporary = self.exchange / f".{job_id}.tmp"
        temporary.write_text(json.dumps({"action": action, **body}))
        temporary.chmod(0o600)
        os.replace(temporary, self.exchange / f"{job_id}.job.json")
        result = self.exchange / f"{job_id}.result.json"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        while loop.time() < deadline:
            if result.exists():
                data = json.loads(result.read_text())
                result.unlink(missing_ok=True)
                return data if isinstance(data, dict) else {"ok": False}
            await asyncio.sleep(self.poll)
        # Never left behind: it may hold a key.
        (self.exchange / f"{job_id}.job.json").unlink(missing_ok=True)
        return {"ok": False, "said": "The remote container did not answer."}


def stage(found: AccessPoint, seen: dict[str, Any] | None) -> str:
    if seen is None:
        return "setup_required"
    backend = str(seen.get("backend") or "")
    if backend in WAITING:
        return "awaiting_authorization" if seen.get("auth_url") else "setup_required"
    if backend != "Running":
        return "connection_problem" if found.enabled else "setup_required"
    if not found.enabled:
        return "connection_problem" if seen.get("routed") else "off"
    if not seen.get("routed"):
        return "connection_problem"
    if found.verified_at is None:
        return "checking"
    return "confirmed_from_phone" if found.confirmed_at else "ready_for_phone_test"


def remote_view(found: AccessPoint, seen: dict[str, Any] | None) -> dict[str, Any]:
    key = stage(found, seen)
    seen = seen or {}
    return {
        "stage": key,
        "label": STAGES[key],
        "installed": bool(seen),
        "backend": seen.get("backend"),
        "dns_name": seen.get("dns_name") or "",
        # A link for the owner to approve this device in their tailnet; it
        # grants nothing to whoever sees it without signing in to Tailscale.
        "auth_url": (seen.get("auth_url") or "") if key == "awaiting_authorization" else "",
        "https": bool(seen.get("https")),
        "hostname": "quarterdeck",
    }
