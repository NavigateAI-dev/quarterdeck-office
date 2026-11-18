"""Direct provider completion for agents assigned a tested connection."""

from typing import Any
from uuid import uuid4

import httpx
import httpx2
import pytest

from app.agent_routing import apply_snapshot
from app.config import Settings
from app.gateway.client import GatewayError
from app.gateway.direct import (
    DirectCompleter,
    UnavailableCompleter,
    anthropic_turns,
    sees_pictures,
)
from app.runtime.worker import build_agents

KEY = "sk-connection-key-0123456789"
MESSAGES = [
    {"role": "system", "content": "Return JSON."},
    {"role": "user", "content": "Objective"},
]


def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://office:secret@localhost/office",
        llm_max_retries=0,
    )


def anthropic_completer(
    seen: list[Any], stop_reason: str = "end_turn", status: int = 200
) -> DirectCompleter:
    def handle(request: Any) -> Any:
        seen.append(request)
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"message": KEY}})
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )

    return DirectCompleter(
        "anthropic",
        "claude-opus-5",
        KEY,
        settings(),
        label="connection:a",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )


def openai_completer(seen: list[Any], finish: str = "stop") -> DirectCompleter:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5-2026",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok": true}'},
                        "finish_reason": finish,
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            },
        )

    return DirectCompleter(
        "openai",
        "gpt-5",
        KEY,
        settings(),
        label="connection:o",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


async def test_anthropic_receives_the_system_prompt_separately() -> None:
    import json

    seen: list[Any] = []
    completion = await anthropic_completer(seen).complete("ignored", MESSAGES)
    body = json.loads(seen[0].content)
    assert body["system"] == "Return JSON."
    assert body["messages"] == [{"role": "user", "content": "Objective"}]
    assert seen[0].headers["x-api-key"] == KEY
    assert completion.content == '{"ok": true}'
    assert completion.model == "claude-opus-5"
    assert completion.model_alias == "connection:a"
    assert completion.usage.prompt_tokens == 12


async def test_openai_reports_the_served_model() -> None:
    seen: list[Any] = []
    completion = await openai_completer(seen).complete("ignored", MESSAGES)
    assert completion.model == "gpt-5-2026"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(
    "stop,code", [("max_tokens", "incomplete_response"), ("refusal", "model_refusal")]
)
async def test_an_unfinished_anthropic_response_is_not_accepted(stop: str, code: str) -> None:
    with pytest.raises(GatewayError, match=code):
        await anthropic_completer([], stop_reason=stop).complete("x", MESSAGES)


async def test_a_truncated_openai_response_is_not_accepted() -> None:
    with pytest.raises(GatewayError, match="incomplete_response"):
        await openai_completer([], finish="length").complete("x", MESSAGES)


async def test_a_rejected_credential_is_a_code_without_the_provider_message() -> None:
    with pytest.raises(GatewayError) as caught:
        await anthropic_completer([], status=401).complete("x", MESSAGES)
    assert caught.value.code == "credential_invalid"
    assert KEY not in str(caught.value)


async def test_an_unavailable_credential_fails_instead_of_falling_back() -> None:
    with pytest.raises(GatewayError, match="credential_unavailable"):
        await UnavailableCompleter("connection:x", "credential_unavailable").complete("x", MESSAGES)


def snapshot_entry(role: str, **overrides: Any) -> dict[str, Any]:
    entry = {
        "profile_id": str(uuid4()),
        "name": "Deep researcher",
        "role": role,
        "version": 3,
        "instructions": "Prefer primary sources.",
        "connection_id": str(uuid4()),
        "provider": "anthropic",
        "model_id": "claude-opus-5",
    }
    return entry | overrides


def test_a_snapshotted_role_uses_its_connection_and_instructions() -> None:
    config = settings()
    gateway: Any = object()
    agents = build_agents(gateway, config)
    entry = snapshot_entry("researcher")
    built: list[dict[str, Any]] = []

    def completer_for(item: dict[str, Any]) -> Any:
        built.append(item)
        return "direct"

    apply_snapshot(agents, {"agents": [entry]}, completer_for)

    assert agents.researcher.gateway == "direct"
    assert agents.researcher.model_alias == f"connection:{entry['connection_id']}"
    assert "Prefer primary sources." in agents.researcher.system_message()
    assert "Deep researcher" in agents.researcher.system_message()
    assert agents.critic.gateway is gateway
    assert built == [entry]


def test_an_agent_without_a_connection_keeps_the_default_model() -> None:
    config = settings()
    gateway: Any = object()
    agents = build_agents(gateway, config)
    apply_snapshot(
        agents, {"agents": [snapshot_entry("critic", connection_id=None)]}, lambda e: "x"
    )
    assert agents.critic.gateway is gateway
    assert "Prefer primary sources." in agents.critic.system_message()


def test_an_empty_snapshot_changes_nothing() -> None:
    config = settings()
    gateway: Any = object()
    agents = build_agents(gateway, config)
    before = agents.chief.system_message()
    apply_snapshot(agents, {}, lambda e: "x")
    assert agents.chief.system_message() == before


def test_every_role_shares_the_run_recorder() -> None:
    from app.agent_routing import correlate
    from app.events import InvocationRecorder

    agents = build_agents(object(), settings())  # type: ignore[arg-type]
    recorder = InvocationRecorder(run_id=uuid4(), task_id=uuid4())
    correlate(agents, recorder)
    assert agents.researcher.recorder is recorder
    assert agents.chief.recorder is recorder


async def test_anthropic_is_asked_for_the_schema_as_structured_output() -> None:
    """Where the provider can guarantee the shape, it is asked to."""
    import json

    from app.schemas.agent_output import Critique

    seen: list[Any] = []
    await anthropic_completer(seen).complete("ignored", MESSAGES, schema=Critique)
    shape = json.loads(seen[0].content)["output_config"]["format"]
    assert shape["type"] == "json_schema"
    assert set(shape["schema"]["properties"]) == set(Critique.model_fields)
    # A constraint structured outputs cannot enforce travels as a description,
    # and the office's own validation still checks it.
    confidence = shape["schema"]["properties"]["confidence"]
    assert "minLength" not in confidence
    assert "minLength" in confidence.get("description", "")


async def test_without_a_schema_anthropic_is_asked_for_nothing_extra() -> None:
    import json

    seen: list[Any] = []
    await anthropic_completer(seen).complete("ignored", MESSAGES)
    assert "output_config" not in json.loads(seen[0].content)


async def test_an_openai_compatible_provider_is_not_sent_the_schema() -> None:
    """Their structured-output support differs; the prompt carries it there."""
    import json

    from app.schemas.agent_output import Critique

    seen: list[Any] = []
    await openai_completer(seen).complete("ignored", MESSAGES, schema=Critique)
    body = json.loads(seen[0].content)
    assert "response_format" not in body and "output_config" not in body


async def test_a_call_that_never_ends_times_out_as_a_whole() -> None:
    """Live finding: three research calls to a provider hung for two hours. A
    per-read timeout never fires on a connection kept alive with blank lines;
    the whole call has a wall-clock limit."""
    import asyncio

    slow = settings().model_copy(update={"llm_timeout_seconds": 0.05})
    completer = DirectCompleter("deepseek", "deepseek-flash", KEY, slow, label="researcher")

    async def forever(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)

    completer._openai = forever  # type: ignore[method-assign]
    with pytest.raises(GatewayError) as stopped:
        await completer.complete("researcher", MESSAGES)
    assert stopped.value.code == "provider_timeout"


def test_an_empty_account_is_named_as_such() -> None:
    """Live finding: DeepSeek answered 402 for an empty balance, which read as a
    generic 'request_rejected' and was blamed on prompt size."""
    from app.providers import classify_status

    assert classify_status(402, None) == "insufficient_balance"
    assert classify_status(400, None) == "request_rejected"


async def test_a_direct_call_is_priced_from_the_catalogue() -> None:
    completer = openai_completer([])
    completer.prices = {"gpt-5": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06}}
    completion = await completer.complete("ignored", MESSAGES)
    assert completion.estimated_cost == 12 * 1e-06 + 5 * 2e-06


async def test_a_direct_call_to_an_unpriced_model_has_no_cost() -> None:
    completer = anthropic_completer([])
    completer.prices = {}
    completion = await completer.complete("ignored", MESSAGES)
    assert completion.estimated_cost is None


def test_a_picture_reaches_anthropic_as_an_image_block() -> None:
    turns = anthropic_turns(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
            }
        ]
    )
    assert turns[0]["content"] == [
        {"type": "text", "text": "Look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
    ]


def test_a_picture_by_address_reaches_anthropic_as_a_url_image() -> None:
    turns = anthropic_turns(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}],
            }
        ]
    )
    assert turns[0]["content"] == [
        {"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}}
    ]


def test_a_connection_sees_when_its_model_reads_pictures() -> None:
    completer = anthropic_completer([])
    completer.prices = {"claude-opus-5": {"supports_vision": True}}
    assert sees_pictures(completer) is True


def test_a_connection_to_an_unknown_model_does_not_see() -> None:
    completer = openai_completer([])
    completer.prices = {}
    assert sees_pictures(completer) is False


def test_an_unavailable_connection_does_not_see() -> None:
    assert sees_pictures(UnavailableCompleter("connection:x", "credential_unavailable")) is False
