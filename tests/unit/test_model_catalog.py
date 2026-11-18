import json
import os
import time
from pathlib import Path
from typing import Any

from app.model_catalog import (
    CACHE_NAME,
    cached_prices,
    call_cost,
    describe,
    describe_all,
    price_map,
)

MAP = {
    "gpt-5": {
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
        "max_input_tokens": 400000,
        "max_output_tokens": 128000,
        "mode": "chat",
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_reasoning": False,
    },
    "gemini/gemini-2.5-pro": {
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
        "max_input_tokens": 1048576,
        "mode": "chat",
    },
}


async def test_prices_and_capabilities_come_from_the_published_map() -> None:
    described = describe(MAP, "openai", "gpt-5")
    assert described["known"] is True
    assert described["input_per_million"] == 1.25
    assert described["output_per_million"] == 10.0
    assert described["context_window"] == 400000
    assert described["capabilities"] == ["tools", "images"]


def test_a_provider_prefix_is_applied_when_looking_a_model_up() -> None:
    assert describe(MAP, "google", "gemini-2.5-pro")["known"] is True
    assert describe(MAP, "openai", "gemini-2.5-pro")["known"] is False


def test_an_unknown_model_says_so_rather_than_guessing() -> None:
    unknown = describe(MAP, "openai", "gpt-9-imaginary")
    assert unknown["known"] is False and "No published price" in unknown["note"]
    local = describe(MAP, "ollama", "llama3.1")
    assert local["local"] is True and "no per-token cost" in local["note"]


async def test_the_map_is_cached_and_reused_without_the_network(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fetch(url: str) -> Any:
        calls.append(url)
        return MAP

    first = await price_map(tmp_path, fetch)
    assert first == MAP and len(calls) == 1
    again = await price_map(tmp_path, fetch)
    assert again == MAP and len(calls) == 1  # served from the cache


async def test_a_failed_refresh_keeps_the_last_copy(tmp_path: Path) -> None:
    cache = tmp_path / "cache" / CACHE_NAME
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(MAP))
    stale = time.time() - 10 * 24 * 60 * 60
    import os

    os.utime(cache, (stale, stale))

    async def failing(url: str) -> Any:
        raise RuntimeError("no network")

    assert await price_map(tmp_path, failing) == MAP


async def test_without_any_copy_models_are_listed_with_price_unknown(tmp_path: Path) -> None:
    async def failing(url: str) -> Any:
        raise RuntimeError("no network")

    catalogue = await price_map(tmp_path, failing)
    described = describe_all(catalogue, "openai", ["gpt-5", "gpt-5-mini"])
    assert [d["known"] for d in described] == [False, False]


def test_a_call_costs_its_tokens_at_the_published_prices() -> None:
    cost = call_cost(MAP, "google", "gemini-2.5-pro", 1000, 100)
    assert cost == 1000 * 1.25e-06 + 100 * 1e-05


def test_a_call_without_a_published_price_has_no_cost() -> None:
    assert call_cost(MAP, "openai", "unknown-model", 1000, 100) is None


def test_a_call_without_usage_has_no_cost() -> None:
    assert call_cost(MAP, "openai", "gpt-5", None, None) is None


def test_a_local_model_costs_nothing() -> None:
    assert call_cost({}, "ollama", "llama3", 1000, 100) == 0.0


def test_cached_prices_read_the_cache_whatever_its_age(tmp_path: Path) -> None:
    path = tmp_path / "cache" / CACHE_NAME
    path.parent.mkdir()
    path.write_text(json.dumps(MAP))
    old = time.time() - 10 * 24 * 60 * 60
    os.utime(path, (old, old))
    assert cached_prices(tmp_path)["gpt-5"]["mode"] == "chat"


def test_cached_prices_are_empty_without_a_cache(tmp_path: Path) -> None:
    assert cached_prices(tmp_path) == {}
