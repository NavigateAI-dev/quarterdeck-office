import pytest


# Settings reads the process environment even when no env file is used, so an
# ambient value would silently change what a test is asserting. The names are
# derived from the settings themselves rather than listed by hand, so a new
# setting cannot quietly escape this and leak into a test.
def _configuration_names() -> frozenset[str]:
    from app.config import Settings

    # Values compose passes that are not application settings.
    extras = {
        "POSTGRES_PASSWORD",
        "CODEX_CREDENTIALS",
        "CHIEF_MODEL",
        "RESEARCH_MODEL",
        "STRATEGY_MODEL",
        "CRITIC_MODEL",
        "CHIEF_API_KEY",
        "RESEARCH_API_KEY",
        "STRATEGY_API_KEY",
        "CRITIC_API_KEY",
    }
    return frozenset({name.upper() for name in Settings.model_fields} | extras)


CONFIGURATION_NAMES = _configuration_names()


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient application configuration for the duration of a test.

    TEST_DATABASE_URL is deliberately preserved: it selects the integration
    database and is not application configuration.
    """
    for name in CONFIGURATION_NAMES:
        if name != "TEST_DATABASE_URL":
            monkeypatch.delenv(name, raising=False)


def git_is_usable() -> bool:
    """Whether git can actually run.

    Presence is not enough: on macOS without an accepted Xcode licence the git
    shim exists and fails on every invocation.
    """
    import subprocess  # noqa: S404 - probing a fixed executable, no shell

    try:
        probe = subprocess.run(  # noqa: S603
            ["git", "--version"],  # noqa: S607
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0
