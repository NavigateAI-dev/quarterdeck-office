"""What every model call returns, and how it fails (shared by every completer).

The LiteLLM gateway client that lived here was retired on 22 Oct 2026
the office calls providers directly
(`app/gateway/direct.py`, `app/office_models.py`).
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GatewayError(Exception):
    """Sanitized failure suitable for reporting without upstream response bodies."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class ToolCall(BaseModel):
    """One tool call a model asked for.

    `arguments` is None when the model's arguments were not a JSON object;
    `raw_arguments` keeps what it sent, so the caller can say what was wrong.
    """

    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str = ""


class Completion(BaseModel):
    model_alias: str
    model: str
    content: str
    usage: Usage
    latency_seconds: float
    estimated_cost: float | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


def tool_call(call_id: str, name: str, raw: str) -> ToolCall:
    """A call from function-style output, whose arguments arrive as JSON text."""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except ValueError:
        parsed = None
    return ToolCall(
        id=call_id,
        name=name,
        arguments=parsed if isinstance(parsed, dict) else None,
        raw_arguments=raw,
    )


def asked(**options: Any) -> dict[str, Any]:
    """Only the options that were given, so a completer that takes none still works."""
    return {name: value for name, value in options.items() if value is not None}


def function_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`{name, description, parameters}` definitions in OpenAI function form."""
    return [{"type": "function", "function": tool} for tool in tools]


def strip_code_fence(content: str) -> str:
    """Remove a Markdown code fence some models wrap around JSON output."""
    text = content.strip()
    if not text.startswith("```"):
        return text
    body = text[3:].removeprefix("json").removeprefix("JSON")
    return body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()
