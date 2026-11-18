import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from app.config import Settings


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://office:secret@localhost/office",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_aliases_and_safe_defaults() -> None:
    config = settings()
    assert list(config.model_aliases.values()) == ["0001", "0002", "0003", "0004"]
    assert not config.enable_code_execution
    assert not config.enable_github_pr
    assert not config.enable_production_deployment
    assert isinstance(config.database_url, SecretStr)
    assert "sk-test-gateway-secret" not in repr(config)


@pytest.mark.parametrize("url", ["sqlite:///local.db", "postgresql://localhost/test", "bad-secret"])
def test_rejects_invalid_database_without_leaking_it(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        settings(database_url=url)
    assert url not in str(caught.value)


def test_production_deployment_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError):
        settings(enable_production_deployment=True)


def test_production_deployment_false_environment_value_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_PRODUCTION_DEPLOYMENT", "false")
    assert settings().enable_production_deployment is False


def test_separate_database_password_preserves_url_components() -> None:
    password = "example@password:/?#%"
    config = settings(
        database_url="postgresql+psycopg://office@postgres/office",
        database_password=password,
    )
    url = make_url(config.database_url.get_secret_value())
    assert url.host == "postgres"
    assert url.password == password
    assert password not in repr(config)


def test_an_unset_engineering_target_is_not_the_current_directory() -> None:
    """Path("") resolves to ".", which would point engineering at this repo."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/x",
        engineering_repository="",
        engineering_test_command="   ",
    )
    assert settings.engineering_repository is None
    assert settings.engineering_test_command is None


def test_an_unset_exchange_folder_is_not_the_current_directory() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/x",
        remote_exchange="",
        fetch_exchange=" ",
    )
    assert settings.remote_exchange is None
    assert settings.fetch_exchange is None


def test_a_configured_engineering_target_is_kept() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/x",
        engineering_repository="/srv/fixture-repo",
    )
    assert str(settings.engineering_repository) == "/srv/fixture-repo"
