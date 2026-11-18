"""Messages on their way to a model, with what must not leave taken out.

Words and tool results only; pictures and ids pass as they are. It runs at the
last step before a provider, so every path to a model goes through it, and what
the office keeps on this computer stays as it was said.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from app.privacy.detect import masked

logger = logging.getLogger("quarterdeck.privacy")


def _deep(value: Any, seen: Counter[str]) -> Any:
    if isinstance(value, str):
        text, done = masked(value)
        seen.update(done)
        return text
    if isinstance(value, list):
        return [_deep(item, seen) for item in value]
    if isinstance(value, dict):
        return {key: _deep(item, seen) for key, item in value.items()}
    return value


def _arguments(raw: Any, seen: Counter[str]) -> Any:
    """A tool call's arguments may be JSON text: masked inside, so it stays JSON."""
    if not isinstance(raw, str):
        return _deep(raw, seen)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _deep(raw, seen)
    return json.dumps(_deep(parsed, seen), ensure_ascii=False)


def _content(content: Any, seen: Counter[str]) -> Any:
    if isinstance(content, list):
        return [
            {**part, "text": _deep(part["text"], seen)}
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            else part
            for part in content
        ]
    return _deep(content, seen) if isinstance(content, str) else content


def outgoing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New messages to send. The given ones are left as they are."""
    seen: Counter[str] = Counter()
    sent = []
    for message in messages:
        copy = {**message}
        if "content" in copy:
            copy["content"] = _content(copy["content"], seen)
        if isinstance(copy.get("tool_calls"), list):
            copy["tool_calls"] = [
                {
                    **call,
                    "function": {
                        **call["function"],
                        "arguments": _arguments(call["function"].get("arguments"), seen),
                    },
                }
                if isinstance(call, dict) and isinstance(call.get("function"), dict)
                else call
                for call in copy["tool_calls"]
            ]
        sent.append(copy)
    if seen:
        # What kinds, never what values.
        logger.info('{"event":"masked_before_sending","kinds":%s}', json.dumps(dict(seen)))
    return sent
