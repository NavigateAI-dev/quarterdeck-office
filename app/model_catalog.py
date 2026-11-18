"""What a model costs and what it can do.

Providers publish prices in their documentation, not in their APIs: an API
key lists model names and nothing else. This reads the price and capability
map maintained by the LiteLLM project (the same gateway the office already
runs), caches it on disk, and refreshes it at most once a day. Without the
network the last cached copy is used; with neither, models are listed with
their price unknown rather than guessed.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)
PRICE_MAP_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
CACHE_NAME = "model_prices.json"
MAX_AGE_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT = 15.0
MILLION = 1_000_000

# How each provider's model names appear in the map.
PREFIX = {
    "openai": "",
    "anthropic": "",
    "google": "gemini/",
    "mistral": "mistral/",
    "groq": "groq/",
    "deepseek": "deepseek/",
    "xai": "xai/",
    "openrouter": "openrouter/",
    "together": "together_ai/",
    "moonshot": "moonshot/",
    "moonshot_cn": "moonshot/",
    "dashscope": "dashscope/",
    "dashscope_cn": "dashscope/",
    "zhipu": "zai/",
    "zhipu_cn": "zai/",
    "minimax": "minimax/",
    "minimax_cn": "minimax/",
    "siliconflow": "",
    "siliconflow_cn": "",
    "ollama": "ollama/",
    "lmstudio": "",
    "gateway": "",
    "custom": "",
}
LOCAL_PROVIDERS = ("ollama", "lmstudio")
CAPABILITIES = {
    "supports_function_calling": "tools",
    "supports_vision": "images",
    "supports_reasoning": "reasoning",
    "supports_prompt_caching": "prompt caching",
    "supports_pdf_input": "PDF input",
    "supports_web_search": "web search",
}


# How each provider is named inside the map, for listing without a key.
LITELLM_PROVIDER = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "mistral": "mistral",
    "groq": "groq",
    "deepseek": "deepseek",
    "xai": "xai",
    "openrouter": "openrouter",
    "together": "together_ai",
    "moonshot": "moonshot",
    "moonshot_cn": "moonshot",
    "dashscope": "dashscope",
    "dashscope_cn": "dashscope",
    "zhipu": "zai",
    "zhipu_cn": "zai",
    "minimax": "minimax",
    "minimax_cn": "minimax",
    "ollama": "ollama",
}
CHAT_MODES = ("chat", "responses")


def models_for_provider(catalogue: dict[str, Any], provider: str) -> list[str]:
    """The provider's published models, from the price list, with no key.

    These are the models the provider documents, not the models a particular
    key may use: an account can be entitled to fewer.
    """
    wanted = LITELLM_PROVIDER.get(provider)
    if wanted is None:
        return []
    prefix = PREFIX.get(provider, "")
    names = []
    for key, entry in catalogue.items():
        if not isinstance(entry, dict) or entry.get("litellm_provider") != wanted:
            continue
        if entry.get("mode") not in CHAT_MODES:
            continue
        names.append(key.removeprefix(prefix) if prefix and key.startswith(prefix) else key)
    return sorted(set(names))


def _cache_path(workspace_root: Path) -> Path:
    return workspace_root / "cache" / CACHE_NAME


def _read_cache(path: Path) -> tuple[dict[str, Any], float] | None:
    try:
        return json.loads(path.read_text()), path.stat().st_mtime
    except (OSError, ValueError):
        return None


async def price_map(workspace_root: Path, fetch: Any = None) -> dict[str, Any]:
    """The price and capability map, from cache when it is fresh enough."""
    path = _cache_path(workspace_root)
    cached = _read_cache(path)
    if cached is not None and time.time() - cached[1] < MAX_AGE_SECONDS:
        return cached[0]
    try:
        if fetch is not None:
            data = await fetch(PRICE_MAP_URL)
        else:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
                response = await client.get(PRICE_MAP_URL)
                response.raise_for_status()
                data = response.json()
        if not isinstance(data, dict):
            raise ValueError("price map is not an object")
    except Exception:  # noqa: BLE001 - the office works without prices
        logger.warning('{"event":"price_map_unavailable"}')
        return cached[0] if cached else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def _entry(catalogue: dict[str, Any], provider: str, model_id: str) -> dict[str, Any] | None:
    candidates = [f"{PREFIX.get(provider, '')}{model_id}", model_id]
    if provider == "openrouter" and "/" in model_id:
        candidates.append(model_id.split("/", 1)[1])
    for key in candidates:
        found = catalogue.get(key)
        if isinstance(found, dict):
            return found
    return None


def cached_prices(workspace_root: Path) -> dict[str, Any]:
    """The last downloaded map, however old, without touching the network.

    For pricing a call as it finishes: a stale price is better than none, and a
    model call never waits on a download.
    """
    cached = _read_cache(_cache_path(workspace_root))
    return cached[0] if cached else {}


def call_cost(
    catalogue: dict[str, Any],
    provider: str,
    model_id: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    """What one call cost at the published prices; None when it cannot be known."""
    if provider in LOCAL_PROVIDERS:
        return 0.0
    if prompt_tokens is None and completion_tokens is None:
        return None
    entry = _entry(catalogue, provider, model_id)
    if entry is None:
        return None
    price_in = entry.get("input_cost_per_token")
    price_out = entry.get("output_cost_per_token")
    if not isinstance(price_in, int | float) or not isinstance(price_out, int | float):
        return None
    return (prompt_tokens or 0) * price_in + (completion_tokens or 0) * price_out


def reads_pictures(catalogue: dict[str, Any], provider: str, model_id: str) -> bool:
    """Whether the model is published as reading images; unknown is no."""
    entry = _entry(catalogue, provider, model_id)
    return bool(entry and entry.get("supports_vision") is True)


def describe(catalogue: dict[str, Any], provider: str, model_id: str) -> dict[str, Any]:
    """Price per million tokens, context sizes and capabilities, where published."""
    entry = _entry(catalogue, provider, model_id)
    local = provider in LOCAL_PROVIDERS
    if entry is None:
        return {
            "model": model_id,
            "known": False,
            "local": local,
            "note": "Runs on your computer; no per-token cost."
            if local
            else "No published price for this name.",
        }
    price_in = entry.get("input_cost_per_token")
    price_out = entry.get("output_cost_per_token")
    return {
        "model": model_id,
        "known": True,
        "local": local,
        "input_per_million": round(price_in * MILLION, 4)
        if isinstance(price_in, int | float)
        else None,
        "output_per_million": round(price_out * MILLION, 4)
        if isinstance(price_out, int | float)
        else None,
        "context_window": entry.get("max_input_tokens") or entry.get("max_tokens"),
        "max_output_tokens": entry.get("max_output_tokens"),
        "capabilities": [
            label for field, label in CAPABILITIES.items() if entry.get(field) is True
        ],
        "mode": entry.get("mode"),
    }


def describe_all(
    catalogue: dict[str, Any], provider: str, models: list[str]
) -> list[dict[str, Any]]:
    return [describe(catalogue, provider, model) for model in models]
