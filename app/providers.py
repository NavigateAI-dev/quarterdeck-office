"""Provider adapters for model connections (OpenAI and Anthropic APIs).

A check has two levels. Discovery lists the account's models, which proves
the credential and the model's availability without a billable request. The
optional test sends one minimal request and is billable, so it runs only when
the owner asks for it. Failures are reported as codes; provider messages are
never carried into results, because they can echo request content.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
import openai

TEST_PROMPT = "Reply with the single word: ok"
TEST_MAX_TOKENS = 16


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None


class ProviderFailure(Exception):
    """A classified provider failure. Carries a code, never a message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProviderAdapter(Protocol):
    provider: str

    async def list_models(self, credential: str) -> list[str]: ...

    async def test_request(self, credential: str, model_id: str) -> Usage: ...


@dataclass(frozen=True)
class ConnectionCheck:
    status: str  # MODEL_AVAILABLE | MODEL_UNAVAILABLE | VERIFIED | FAILED
    billable: bool
    latency_ms: int
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def classify_status(status: int, code: str | None) -> str:
    if status == 401:
        return "credential_invalid"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "model_unavailable"
    if status == 402:
        # An empty account, not a bad request: DeepSeek says 402 when the
        # balance is gone, and every call fails until it is topped up.
        return "insufficient_balance"
    if status == 429:
        return "quota_exhausted" if code == "insufficient_quota" else "rate_limited"
    if status >= 500:
        return "provider_unavailable"
    return "request_rejected"


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(
        self,
        http_client: Any = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        base_url: str | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.base_url = base_url

    def _client(self, credential: str) -> anthropic.AsyncAnthropic:
        # The key is always explicit, so no ambient environment variable or
        # CLI profile can stand in for the connection's own credential.
        return anthropic.AsyncAnthropic(
            api_key=credential,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=self.max_retries,
            http_client=self.http_client,
        )

    async def list_models(self, credential: str) -> list[str]:
        try:
            return [model.id async for model in self._client(credential).models.list()]
        except anthropic.APIError as error:
            raise ProviderFailure(self._classify(error)) from None

    async def test_request(self, credential: str, model_id: str) -> Usage:
        try:
            message = await self._client(credential).messages.create(
                model=model_id,
                max_tokens=TEST_MAX_TOKENS,
                messages=[{"role": "user", "content": TEST_PROMPT}],
            )
        except anthropic.APIError as error:
            raise ProviderFailure(self._classify(error)) from None
        return Usage(message.usage.input_tokens, message.usage.output_tokens)

    @staticmethod
    def _classify(error: anthropic.APIError) -> str:
        if isinstance(error, anthropic.APITimeoutError):
            return "provider_timeout"
        if isinstance(error, anthropic.APIConnectionError):
            return "provider_unreachable"
        if isinstance(error, anthropic.APIStatusError):
            return classify_status(error.status_code, None)
        return "provider_error"


class OpenAIAdapter:
    provider = "openai"

    def __init__(
        self,
        http_client: Any = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        base_url: str | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.base_url = base_url

    def _client(self, credential: str) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key=credential,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=self.max_retries,
            http_client=self.http_client,
        )

    async def list_models(self, credential: str) -> list[str]:
        try:
            return [model.id async for model in self._client(credential).models.list()]
        except openai.APIError as error:
            raise ProviderFailure(self._classify(error)) from None

    async def test_request(self, credential: str, model_id: str) -> Usage:
        try:
            completion = await self._client(credential).chat.completions.create(
                model=model_id,
                max_completion_tokens=TEST_MAX_TOKENS,
                messages=[{"role": "user", "content": TEST_PROMPT}],
            )
        except openai.APIError as error:
            raise ProviderFailure(self._classify(error)) from None
        usage = completion.usage
        if usage is None:
            return Usage(None, None)
        return Usage(usage.prompt_tokens, usage.completion_tokens)

    @staticmethod
    def _classify(error: openai.APIError) -> str:
        if isinstance(error, openai.APITimeoutError):
            return "provider_timeout"
        if isinstance(error, openai.APIConnectionError):
            return "provider_unreachable"
        if isinstance(error, openai.APIStatusError):
            return classify_status(error.status_code, error.code)
        return "provider_error"


@dataclass(frozen=True)
class Preset:
    """A provider the owner can connect to, and how to reach it.

    Everything except Anthropic speaks the OpenAI chat API, so one adapter
    serves them all; only the address and the example models differ.
    """

    id: str
    name: str
    kind: str  # "anthropic" | "openai"
    base_url: str | None
    about: str
    examples: tuple[str, ...] = ()
    needs_base_url: bool = False
    needs_key: bool = True


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset("openai", "OpenAI", "openai", None, "Your OpenAI account.", ("gpt-5", "gpt-5-mini")),
        Preset(
            "anthropic",
            "Anthropic",
            "anthropic",
            None,
            "Your Anthropic account.",
            ("claude-opus-5", "claude-sonnet-5"),
        ),
        Preset(
            "google",
            "Google AI (Gemini)",
            "openai",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "Gemini models through Google's OpenAI-compatible endpoint.",
            ("gemini-2.5-pro", "gemini-2.5-flash"),
        ),
        Preset(
            "mistral",
            "Mistral",
            "openai",
            "https://api.mistral.ai/v1",
            "Mistral's hosted models.",
            ("mistral-large-latest",),
        ),
        Preset(
            "groq",
            "Groq",
            "openai",
            "https://api.groq.com/openai/v1",
            "Fast hosted open models.",
            ("llama-3.3-70b-versatile",),
        ),
        Preset(
            "deepseek",
            "DeepSeek",
            "openai",
            "https://api.deepseek.com",
            "DeepSeek's hosted models.",
            ("deepseek-chat", "deepseek-reasoner"),
        ),
        Preset(
            "xai",
            "xAI (Grok)",
            "openai",
            "https://api.x.ai/v1",
            "xAI's hosted models.",
            ("grok-4",),
        ),
        Preset(
            "openrouter",
            "OpenRouter",
            "openai",
            "https://openrouter.ai/api/v1",
            "One key, many providers' models.",
            ("anthropic/claude-sonnet-5", "openai/gpt-5"),
        ),
        Preset(
            "together",
            "Together AI",
            "openai",
            "https://api.together.xyz/v1",
            "Hosted open models.",
            ("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
        ),
        # Asian model providers: each speaks the OpenAI chat API. Where
        # an account is mainland China's, the provider runs a second address.
        Preset(
            "moonshot",
            "Moonshot AI (Kimi)",
            "openai",
            "https://api.moonshot.ai/v1",
            "Kimi models, for an account made at platform.moonshot.ai.",
            ("kimi-k2-turbo-preview", "kimi-latest"),
        ),
        Preset(
            "moonshot_cn",
            "Moonshot AI (Kimi), China",
            "openai",
            "https://api.moonshot.cn/v1",
            "Kimi models, for an account made at platform.moonshot.cn.",
            ("kimi-k2-turbo-preview", "kimi-latest"),
        ),
        Preset(
            "dashscope",
            "Alibaba Cloud (Qwen)",
            "openai",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "Qwen models through Model Studio, international (Singapore).",
            ("qwen-max", "qwen-plus"),
        ),
        Preset(
            "dashscope_cn",
            "Alibaba Cloud (Qwen), China",
            "openai",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "Qwen models through Bailian, for a mainland China account.",
            ("qwen-max", "qwen-plus"),
        ),
        Preset(
            "zhipu",
            "Z.ai (GLM)",
            "openai",
            "https://api.z.ai/api/paas/v4",
            "GLM models, for an account made at z.ai.",
            ("glm-4.6", "glm-4.5-air"),
        ),
        Preset(
            "zhipu_cn",
            "Zhipu AI (GLM), China",
            "openai",
            "https://open.bigmodel.cn/api/paas/v4",
            "GLM models, for an account made at bigmodel.cn.",
            ("glm-4.6", "glm-4.5-air"),
        ),
        Preset(
            "minimax",
            "MiniMax",
            "openai",
            "https://api.minimax.io/v1",
            "MiniMax models, for an account made at minimax.io.",
            ("MiniMax-M2",),
        ),
        Preset(
            "minimax_cn",
            "MiniMax, China",
            "openai",
            "https://api.minimaxi.com/v1",
            "MiniMax models, for an account made at minimaxi.com.",
            ("MiniMax-M2",),
        ),
        Preset(
            "siliconflow",
            "SiliconFlow",
            "openai",
            "https://api.siliconflow.com/v1",
            "Many open models (DeepSeek, Qwen, GLM, Kimi) on one key, at siliconflow.com.",
            ("deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B"),
        ),
        Preset(
            "siliconflow_cn",
            "SiliconFlow, China",
            "openai",
            "https://api.siliconflow.cn/v1",
            "Many open models on one key, for an account made at siliconflow.cn.",
            ("deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B"),
        ),
        Preset(
            "ollama",
            "Ollama (this computer)",
            "openai",
            "http://host.docker.internal:11434/v1",
            "Models running on your own machine. No key needed.",
            ("llama3.1", "qwen2.5"),
            needs_key=False,
        ),
        Preset(
            "lmstudio",
            "LM Studio (this computer)",
            "openai",
            "http://host.docker.internal:1234/v1",
            "Models running in LM Studio. No key needed.",
            (),
            needs_key=False,
        ),
        Preset(
            "custom",
            "Another OpenAI-compatible service",
            "openai",
            None,
            "Anything that speaks the OpenAI chat API: Azure OpenAI, vLLM, a proxy of your own.",
            (),
            needs_base_url=True,
        ),
    )
}


# Model families that are not chat models. A provider's listing mixes in
# embeddings, speech and image models, which an agent cannot use.
NOT_CHAT = re.compile(
    r"(embed|embedding|whisper|tts|audio|speech|transcribe|dall-e|image|vision-encoder|"
    r"moderation|rerank|clip|guard|safety|search-|similarity|edit-|davinci-instruct|"
    r"babbage|ada-|curie|codex-mini)",
    re.IGNORECASE,
)


def likely_chat_models(models: list[str]) -> list[str]:
    """The models an agent could plausibly talk to.

    A filter, not a judgement: the full list is always returned alongside it,
    because only the provider knows for certain.
    """
    return [model for model in models if not NOT_CHAT.search(model)]


def preset_for(provider: str) -> Preset:
    preset = PRESETS.get(provider)
    if preset is None:
        raise ProviderFailure("provider_unsupported")
    return preset


def adapter_for(provider: str, base_url: str | None = None) -> ProviderAdapter:
    preset = preset_for(provider)
    address = base_url or preset.base_url
    if preset.kind == "anthropic":
        return AnthropicAdapter(base_url=address)
    return OpenAIAdapter(base_url=address)


async def check_connection(
    adapter: ProviderAdapter, credential: str, model_id: str, billable: bool
) -> ConnectionCheck:
    """Discover models, then optionally make one billable test request.

    No fallback to another provider or model is ever attempted.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        models = await adapter.list_models(credential)
        if model_id not in models:
            return ConnectionCheck("MODEL_UNAVAILABLE", False, elapsed(), error="model_unavailable")
        if not billable:
            return ConnectionCheck("MODEL_AVAILABLE", False, elapsed())
        usage = await adapter.test_request(credential, model_id)
    except ProviderFailure as failure:
        return ConnectionCheck("FAILED", billable, elapsed(), error=failure.code)
    return ConnectionCheck(
        "VERIFIED",
        True,
        elapsed(),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
