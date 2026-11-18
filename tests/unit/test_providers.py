"""Provider adapters against mocked HTTP. No real provider is contacted."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import pytest

from app.providers import (
    AnthropicAdapter,
    OpenAIAdapter,
    ProviderAdapter,
    check_connection,
)

KEY = "sk-connection-key-0123456789"
Handler = Callable[[Any], Any]


def anthropic_adapter(handler: Handler) -> AnthropicAdapter:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AnthropicAdapter(http_client=client, max_retries=0)


def openai_adapter(handler: Handler) -> OpenAIAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIAdapter(http_client=client, max_retries=0)


def anthropic_handler(
    seen: list[Any], status: int = 200, body: dict[str, Any] | None = None
) -> Handler:
    def handle(request: Any) -> Any:
        seen.append(request)
        if status != 200:
            return httpx2.Response(status, json=body or {"type": "error", "error": {}})
        if request.url.path.endswith("/models"):
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "claude-opus-5",
                            "type": "model",
                            "display_name": "Claude Opus 5",
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                    "has_more": False,
                    "first_id": "claude-opus-5",
                    "last_id": "claude-opus-5",
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 8, "output_tokens": 1},
            },
        )

    return handle


def openai_handler(
    seen: list[Any], status: int = 200, body: dict[str, Any] | None = None
) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json=body or {"error": {"message": "x"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "gpt-5", "object": "model", "created": 0, "owned_by": "o"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
            },
        )

    return handle


async def test_discovery_confirms_the_model_without_a_billable_request() -> None:
    seen: list[Any] = []
    result = await check_connection(
        anthropic_adapter(anthropic_handler(seen)), KEY, "claude-opus-5", billable=False
    )
    assert result.status == "MODEL_AVAILABLE"
    assert result.billable is False
    assert [request.method for request in seen] == ["GET"]
    assert seen[0].headers["x-api-key"] == KEY


async def test_an_unlisted_model_is_reported_unavailable() -> None:
    result = await check_connection(
        anthropic_adapter(anthropic_handler([])), KEY, "claude-missing", billable=False
    )
    assert result.status == "MODEL_UNAVAILABLE"
    assert result.error == "model_unavailable"


async def test_a_billable_test_is_made_only_when_requested_and_reports_usage() -> None:
    seen: list[Any] = []
    result = await check_connection(
        anthropic_adapter(anthropic_handler(seen)), KEY, "claude-opus-5", billable=True
    )
    assert result.status == "VERIFIED"
    assert result.billable is True
    assert result.input_tokens == 8
    assert result.output_tokens == 1
    assert json.loads(seen[1].content)["model"] == "claude-opus-5"


async def test_openai_uses_the_connection_key() -> None:
    seen: list[Any] = []
    result = await check_connection(
        openai_adapter(openai_handler(seen)), KEY, "gpt-5", billable=True
    )
    assert result.status == "VERIFIED"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert result.input_tokens == 8


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "credential_invalid"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
    ],
)
@pytest.mark.parametrize("make", ["anthropic", "openai"])
async def test_provider_errors_become_distinct_codes(status: int, code: str, make: str) -> None:
    adapter: ProviderAdapter = (
        anthropic_adapter(anthropic_handler([], status))
        if make == "anthropic"
        else openai_adapter(openai_handler([], status))
    )
    result = await check_connection(adapter, KEY, "any", billable=False)
    assert result.status == "FAILED"
    assert result.error == code


async def test_exhausted_openai_quota_is_not_a_rate_limit() -> None:
    body = {
        "error": {"message": "quota", "type": "insufficient_quota", "code": "insufficient_quota"}
    }
    result = await check_connection(
        openai_adapter(openai_handler([], 429, body)), KEY, "gpt-5", billable=False
    )
    assert result.error == "quota_exhausted"


async def test_provider_messages_never_reach_the_result() -> None:
    body = {"type": "error", "error": {"type": "x", "message": f"bad key {KEY}"}}
    result = await check_connection(
        anthropic_adapter(anthropic_handler([], 401, body)), KEY, "claude-opus-5", billable=False
    )
    assert KEY not in repr(result)


async def test_an_unreachable_provider_is_reported() -> None:
    def refuse(request: Any) -> Any:
        raise httpx.ConnectError("refused", request=request)

    result = await check_connection(openai_adapter(refuse), KEY, "gpt-5", billable=False)
    assert result.error == "provider_unreachable"
