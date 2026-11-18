"""Slack Socket Mode: receive owner messages and button presses without a public URL.

The app-level token (xapp-, scope connections:write) opens a WebSocket URL.
Every envelope is acknowledged by its envelope_id before it is handled, so
Slack does not redeliver it while a slow judgment runs; redeliveries that do
arrive are dropped by event_id in the handler.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

from app.channels.inbound import IDEA_REACTION, InboundRequest
from app.channels.slack_format import ACTIONS

log = logging.getLogger(__name__)
MENTION = re.compile(r"<@[A-Z0-9_]+>")
STARTUP_SECONDS = 5
OPEN_URL = "https://slack.com/api/apps.connections.open"


def request_from_event(payload: dict[str, Any], bot_user_id: str) -> InboundRequest | None:
    """A direct message, an @mention, or a 💡 on a message, from a person;
    everything else is None."""
    event = payload.get("event") or {}
    kind = event.get("type")
    if kind == "reaction_added":
        return _reaction(payload, event, bot_user_id)
    # A message with files arrives as file_share; every other subtype
    # (edits, joins, bot posts) is not a person asking.
    if event.get("subtype") not in (None, "file_share"):
        return None
    if event.get("bot_id") or event.get("user") in (None, bot_user_id):
        return None
    if kind == "message" and event.get("channel_type") != "im":
        return None
    if kind not in ("message", "app_mention"):
        return None
    return InboundRequest(
        workspace_id=str(payload.get("team_id", "")),
        user_id=str(event["user"]),
        channel_id=str(event.get("channel", "")),
        text=MENTION.sub("", str(event.get("text", ""))).strip(),
        event_id=str(payload.get("event_id", "")),
        thread_ts=event.get("thread_ts") or event.get("ts"),
        files=tuple(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "file")[:200],
                "mimetype": str(item.get("mimetype") or ""),
                "size": int(item.get("size") or 0),
                "url": str(item.get("url_private_download") or item.get("url_private") or ""),
            }
            for item in (event.get("files") or [])[:4]
            if isinstance(item, dict)
        ),
    )


def _reaction(
    payload: dict[str, Any], event: dict[str, Any], bot_user_id: str
) -> InboundRequest | None:
    """A 💡 put on a message; other reactions, and reactions on files, are None."""
    item = event.get("item") or {}
    user = str(event.get("user") or "")
    if event.get("reaction") != IDEA_REACTION or item.get("type") != "message":
        return None
    if not user or user == bot_user_id or not item.get("channel") or not item.get("ts"):
        return None
    return InboundRequest(
        workspace_id=str(payload.get("team_id", "")),
        user_id=user,
        channel_id=str(item["channel"]),
        text="",
        event_id=str(payload.get("event_id", "")),
        thread_ts=None,
        message_ts=str(item["ts"]),
        reaction=IDEA_REACTION,
    )


def request_from_action(payload: dict[str, Any]) -> InboundRequest | None:
    """A button the office posted, pressed by a person; anything else is None.

    Link buttons (Open in the office) also report a click; they need nothing.
    """
    if payload.get("type") != "block_actions":
        return None
    pressed = (payload.get("actions") or [{}])[0]
    action = str(pressed.get("action_id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    if action not in ACTIONS or not user:
        return None
    message = payload.get("message") or {}
    container = payload.get("container") or {}
    channel = str((payload.get("channel") or {}).get("id") or container.get("channel_id") or "")
    ts = str(container.get("message_ts") or message.get("ts") or "")
    return InboundRequest(
        workspace_id=str((payload.get("team") or {}).get("id") or ""),
        user_id=user,
        channel_id=channel,
        text="",
        event_id=f"action:{pressed.get('action_ts') or ts}:{user}",
        thread_ts=message.get("thread_ts") or container.get("thread_ts") or ts or None,
        action=action,
        value=str(pressed.get("value") or ""),
        message_ts=ts or None,
        message_text=str(message.get("text") or ""),
    )


async def process_frame(
    frame: str | bytes,
    bot_user_id: str,
    ack: Callable[[str], Awaitable[None]],
    handle: Callable[[InboundRequest], Awaitable[None]],
) -> str:
    message = json.loads(frame)
    kind = message.get("type")
    if kind == "disconnect":
        return "reconnect"
    envelope = message.get("envelope_id")
    if envelope:
        await ack(json.dumps({"envelope_id": envelope}))
    if kind == "interactive":
        request = request_from_action(message.get("payload") or {})
    elif kind == "events_api":
        request = request_from_event(message.get("payload") or {}, bot_user_id)
    else:
        return "ignored"
    if request is None:
        return "ignored"
    await handle(request)
    return "handled"


async def open_url(app_token: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(OPEN_URL, headers={"Authorization": f"Bearer {app_token}"})
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"socket_mode_open_failed:{body.get('error', 'unknown')}")
    return str(body["url"])


async def listen(
    app_token: Callable[[], str | None],
    bot_user_id: Callable[[], str | None],
    handle: Callable[[InboundRequest], Awaitable[None]],
    stop: asyncio.Event,
) -> None:
    """Stay connected while tokens are configured; back off on failure."""
    delay = 1.0
    while not stop.is_set():
        try:
            token, bot = app_token(), bot_user_id()
        except Exception:  # noqa: BLE001 - configuration unreadable; try again later
            log.exception("socket_mode_configuration_unavailable")
            token = bot = None
        if not token or not bot:
            await _wait(stop, 30)
            continue
        try:
            async with websockets.connect(await open_url(token)) as socket:
                delay = 1.0
                async for frame in socket:
                    if stop.is_set():
                        return
                    try:
                        outcome = await process_frame(frame, bot, socket.send, handle)
                    except Exception:  # A bad message must not drop the connection.
                        log.exception("socket_mode_frame_failed")
                        continue
                    if outcome == "reconnect":
                        break
        except Exception as error:  # noqa: BLE001 - logged, retried with backoff
            log.warning("socket_mode_disconnected: %s", type(error).__name__)
            await _wait(stop, delay)
            delay = min(delay * 2, 60)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass
