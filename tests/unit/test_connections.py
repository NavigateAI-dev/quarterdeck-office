import pytest
from pydantic import ValidationError

from app.api.schemas import ModelConnectionCreate
from app.config import Settings


def test_model_connection_accepts_a_local_secret_reference() -> None:
    connection = ModelConnectionCreate(
        provider="openai",
        display_name="OpenAI work account",
        model_id="gpt-5",
        credential_ref="env:OPENAI_API_KEY",
        capabilities=["structured_output", "web_search"],
    )
    assert connection.credential_ref == "env:OPENAI_API_KEY"


@pytest.mark.parametrize("credential_ref", ["sk-live-key", "file:/tmp/key", "env:lowercase"])
def test_model_connection_rejects_raw_or_unsafe_secret_references(credential_ref: str) -> None:
    with pytest.raises(ValidationError):
        ModelConnectionCreate(
            provider="anthropic",
            display_name="Anthropic",
            model_id="claude-sonnet",
            credential_ref=credential_ref,
        )


def test_model_connection_rejects_a_malformed_provider_name() -> None:
    for provider in ("Mystery Corp", "", "1provider"):
        with pytest.raises(ValidationError):
            ModelConnectionCreate(
                provider=provider,
                display_name="Custom",
                model_id="model",
                credential_ref="env:CUSTOM_API_KEY",
            )


def test_a_base_url_must_be_a_web_address() -> None:
    with pytest.raises(ValidationError):
        ModelConnectionCreate(
            provider="custom",
            display_name="Custom",
            model_id="model",
            base_url="file:///etc/passwd",
        )
    accepted = ModelConnectionCreate(
        provider="custom",
        display_name="Custom",
        model_id="model",
        base_url="https://models.example.com/v1",
    )
    assert accepted.base_url == "https://models.example.com/v1"


def test_every_provider_in_the_catalogue_resolves_to_an_adapter() -> None:
    from app.providers import PRESETS, adapter_for, preset_for

    for provider, preset in PRESETS.items():
        adapter = adapter_for(provider, None)
        assert adapter.provider in ("openai", "anthropic")
        # A service with no fixed address must be given one by the owner.
        assert preset.needs_base_url == (
            preset.base_url is None and provider not in ("openai", "anthropic")
        )
        assert preset_for(provider) is preset


def office_settings() -> Settings:
    """Settings without reading the machine's .env, as elsewhere in the suite."""
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://office:secret@localhost/office",
    )  # type: ignore[call-arg]


def test_a_direct_completer_accepts_any_catalogued_provider() -> None:
    from app.gateway.client import GatewayError
    from app.gateway.direct import DirectCompleter
    from app.providers import PRESETS

    settings = office_settings()
    for provider, preset in PRESETS.items():
        completer = DirectCompleter(provider, "m", "k", settings, label="l")
        assert completer.provider == preset.kind
        assert completer.base_url == preset.base_url
    with pytest.raises(GatewayError):
        DirectCompleter("nobody", "m", "k", settings, label="l")


def test_a_custom_base_url_overrides_the_catalogue() -> None:
    from app.gateway.direct import DirectCompleter

    completer = DirectCompleter(
        "groq", "m", "k", office_settings(), label="l", base_url="https://proxy.example.com/v1"
    )
    assert completer.base_url == "https://proxy.example.com/v1"
