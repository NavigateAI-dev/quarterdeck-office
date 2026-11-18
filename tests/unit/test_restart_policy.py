"""The office comes back by itself after Docker restarts."""

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.mark.parametrize("service", ["postgres", "api"])
def test_the_office_and_its_database_restart_with_docker(service: str) -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert services[service]["restart"] == "unless-stopped"
