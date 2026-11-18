"""Direct provider completion for agents assigned a tested model connection.

The default path sends a logical alias through the LiteLLM gateway. An agent
profile that names a connection instead calls that provider with the
connection's own credential. Failures map onto the gateway's error codes so
the agent contract and run handling are identical on either path, and no
failure ever falls back to another provider.
"""

import asyncio
import json
from time import monotonic
from typing import Any, NamedTuple, Protocol

import anthropic
import openai
from pydantic import BaseModel

from app.config import Settings
from app.gateway.client import (
    Completion,
    GatewayError,
    ToolCall,
    Usage,
    function_tools,
    tool_call,
)
from app.model_catalog import cached_prices, call_cost, reads_pictures
from app.privacy.outbound import outgoing
from app.providers import ProviderFailure, classify_status, preset_for


class Completer(Protocol):
    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion: ...


class _Answer(NamedTuple):
    content: str
    model: str
    usage: Usage
    calls: list[ToolCall]


class UnavailableCompleter:
    """Stands in when a connection's credential cannot be resolved.

    The run fails at the agent that needed it, with a code, rather than
    quietly using a different model.
    """

    sees = False

    def __init__(self, label: str, code: str) -> None:
        self.label = label
        self.code = code

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        raise GatewayError(self.code)


class DirectCompleter:
    def __init__(
        self,
        provider: str,
        model_id: str,
        credential: str,
        settings: Settings,
        label: str,
        http_client: Any = None,
        base_url: str | None = None,
        prices: dict[str, Any] | None = None,
    ) -> None:
        try:
            preset = preset_for(provider)
        except ProviderFailure:
            raise GatewayError("provider_unsupported") from None
        self.provider = preset.kind
        self.provider_name = provider
        self.base_url = base_url or preset.base_url
        self.model_id = model_id
        self.credential = credential
        self.settings = settings
        self.label = label
        self.http_client = http_client
        # The price catalogue; read from its cache at the first call when not given.
        self.prices = prices

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        started = monotonic()
        messages = outgoing(messages)
        cap = max_tokens or self.settings.llm_max_output_tokens
        # A wall-clock limit on the whole call, retries included. The SDKs'
        # timeout bounds each read, and a provider that keeps the connection
        # alive with blank lines never trips it: three research calls hung for
        # two hours on the live office, and with them the office's worker.
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                if self.provider == "anthropic":
                    answer = await self._anthropic(messages, cap, schema, tools)
                else:
                    answer = await self._openai(messages, cap, tools)
        except TimeoutError:
            raise GatewayError("provider_timeout") from None
        if not answer.calls and not answer.content.strip():
            raise GatewayError("empty_response")
        return Completion(
            model_alias=self.label,
            model=answer.model,
            content=answer.content,
            usage=answer.usage,
            latency_seconds=monotonic() - started,
            estimated_cost=self._cost(answer.usage),
            tool_calls=answer.calls,
        )

    def _catalogue(self) -> dict[str, Any]:
        if self.prices is None:
            self.prices = cached_prices(self.settings.workspace_root)
        return self.prices

    @property
    def sees(self) -> bool:
        """Whether this model reads pictures, as its provider publishes."""
        return reads_pictures(self._catalogue(), self.provider_name, self.model_id)

    def _cost(self, usage: Usage) -> float | None:
        """The call's cost at the published price of the model asked for."""
        return call_cost(
            self._catalogue(),
            self.provider_name,
            self.model_id,
            usage.prompt_tokens,
            usage.completion_tokens,
        )

    async def _anthropic(
        self,
        messages: list[dict[str, Any]],
        cap: int,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> _Answer:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns: Any = anthropic_turns(messages)
        client = anthropic.AsyncAnthropic(
            api_key=self.credential,
            base_url=self.base_url,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
            http_client=self.http_client,
        )
        # Where this provider can guarantee the shape, it is asked to: the answer
        # then cannot fall outside the schema. The schema still reaches the model
        # as prose for every other provider, and the answer is validated either
        # way. Constraints structured outputs cannot enforce are carried into the
        # schema's descriptions, and checked by that validation.
        shape: Any = (
            {
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(schema.model_json_schema()),
                }
            }
            if schema is not None
            else anthropic.omit
        )
        offered: Any = anthropic_tools(tools) if tools else anthropic.omit
        try:
            message = await client.messages.create(
                model=self.model_id,
                max_tokens=cap,
                system=system,
                messages=turns,
                output_config=shape,
                tools=offered,
            )
        except anthropic.APITimeoutError:
            raise GatewayError("provider_timeout") from None
        except anthropic.APIConnectionError:
            raise GatewayError("provider_unreachable") from None
        except anthropic.APIStatusError as error:
            raise GatewayError(
                classify_status(error.status_code, None), error.status_code
            ) from None
        if message.stop_reason == "refusal":
            raise GatewayError("model_refusal")
        calls = [
            ToolCall(
                id=block.id,
                name=block.name,
                arguments=block.input if isinstance(block.input, dict) else None,
                raw_arguments=json.dumps(block.input),
            )
            for block in message.content
            if block.type == "tool_use"
        ]
        if not (message.stop_reason == "end_turn" or (message.stop_reason == "tool_use" and calls)):
            raise GatewayError("incomplete_response")
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = Usage(
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
        )
        return _Answer(text, message.model, usage, calls)

    async def _openai(
        self, messages: list[dict[str, Any]], cap: int, tools: list[dict[str, Any]] | None = None
    ) -> _Answer:
        client = openai.AsyncOpenAI(
            api_key=self.credential,
            base_url=self.base_url,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
            http_client=self.http_client,
        )
        turns: Any = messages
        offered: Any = function_tools(tools) if tools else openai.omit
        try:
            completion = await client.chat.completions.create(
                model=self.model_id,
                max_completion_tokens=cap,
                messages=turns,
                tools=offered,
            )
        except openai.APITimeoutError:
            raise GatewayError("provider_timeout") from None
        except openai.APIConnectionError:
            raise GatewayError("provider_unreachable") from None
        except openai.APIStatusError as error:
            raise GatewayError(
                classify_status(error.status_code, error.code), error.status_code
            ) from None
        choice = completion.choices[0]
        if choice.message.refusal or choice.finish_reason == "content_filter":
            raise GatewayError("model_refusal")
        calls = [
            tool_call(call.id, call.function.name, call.function.arguments)
            for call in choice.message.tool_calls or []
            if call.type == "function"
        ]
        if not (choice.finish_reason == "stop" or (choice.finish_reason == "tool_calls" and calls)):
            raise GatewayError("incomplete_response")
        usage = Usage(
            prompt_tokens=completion.usage.prompt_tokens if completion.usage else None,
            completion_tokens=completion.usage.completion_tokens if completion.usage else None,
        )
        return _Answer(choice.message.content or "", completion.model, usage, calls)


def sees_pictures(completer: Any) -> bool:
    """Whether a completer's model reads pictures; anything unknown does not."""
    return getattr(completer, "sees", False) is True


def anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`{name, description, parameters}` definitions in Anthropic's form."""
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in tools
    ]


def anthropic_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation without its system prompt, tool turns in Anthropic's form.

    An assistant turn's function calls become `tool_use` blocks; consecutive
    tool results become one user turn of `tool_result` blocks; pictures become
    image blocks. Every other turn passes unchanged.
    """
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                function = call["function"]
                parsed = tool_call(call["id"], function["name"], function.get("arguments") or "")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": function["name"],
                        "input": parsed.arguments or {},
                    }
                )
            turns.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": message["content"],
            }
            last = turns[-1] if turns else None
            if last and last["role"] == "user" and _results_only(last["content"]):
                last["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
        elif isinstance(message.get("content"), list):
            turns.append({**message, "content": [_anthropic_part(p) for p in message["content"]]})
        else:
            turns.append(message)
    return turns


def _anthropic_part(part: Any) -> Any:
    """An `image_url` part as Anthropic's image block; any other part unchanged."""
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return part
    url = str((part.get("image_url") or {}).get("url") or "")
    header, comma, data = url.partition(",")
    if url.startswith("data:") and comma and header.endswith(";base64"):
        media_type = header.removeprefix("data:").removesuffix(";base64")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _results_only(content: Any) -> bool:
    return isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )
