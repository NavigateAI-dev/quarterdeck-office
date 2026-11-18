from pathlib import Path

import yaml

# Model keys are not settings: they live in the office's encrypted store. The
# gateway-era role keys must never reach a service's environment. SEARCH_API_KEY
# is application code's own key, not a model credential.
PROVIDER_MODEL_KEYS = frozenset(
    {"CHIEF_API_KEY", "RESEARCH_API_KEY", "STRATEGY_API_KEY", "CRITIC_API_KEY"}
)


def test_provider_keys_are_not_passed_to_application() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    for name in ("api", "migrate", "postgres"):
        environment = compose["services"][name]["environment"]
        assert not PROVIDER_MODEL_KEYS.intersection(environment)
        # Role models are connections now, chosen in the browser. CODEX_MODEL is
        # not one: the application spawns the engineering runtime itself.
        assert not {f"{role}_MODEL" for role in ("CHIEF", "RESEARCH", "STRATEGY", "CRITIC")} & set(
            environment
        )
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            # Only the office itself may be published on the Mac's network,
            # and only when the owner runs `scripts.access home`.
            allowed = (
                ("127.0.0.1:", "${OFFICE_BIND:-127.0.0.1}:") if name == "api" else ("127.0.0.1:",)
            )
            assert port.startswith(allowed), (name, port)


def test_the_litellm_gateway_is_gone() -> None:
    """Retired on 22 Oct 2026: no service, no setting."""
    compose = Path("docker-compose.yml").read_text()
    assert "litellm" not in compose.lower()
    assert "LITELLM" not in Path(".env.example").read_text()
    assert not Path("litellm").exists()
    assert not Path("scripts/start_gateway.py").exists()


def test_every_setting_can_be_configured_in_the_container() -> None:
    """A setting absent from compose cannot be changed without rebuilding."""
    from app.config import Settings

    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    provided = set(compose["services"]["api"]["environment"])
    # The password is passed separately and folded into DATABASE_URL by the
    # application.
    expected = {
        name.upper() for name in Settings.model_fields if name.upper() not in {"DATABASE_PASSWORD"}
    }
    assert expected <= provided


def test_the_environment_template_documents_every_setting() -> None:
    from app.config import Settings

    template = Path(".env.example").read_text()
    documented = {line.split("=")[0] for line in template.splitlines() if "=" in line}
    missing = {
        name.upper()
        for name in Settings.model_fields
        if name.upper() not in documented and name.upper() != "DATABASE_PASSWORD"
    }
    assert missing == set()


def test_service_images_pin_their_build_stage() -> None:
    """The test stage is last in the Dockerfile, so an unpinned build picks it."""
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    expected = {"migrate": "runtime", "api": "runtime", "tests": "test"}
    for name, target in expected.items():
        build = compose["services"][name]["build"]
        assert isinstance(build, dict), f"{name} must pin a build target"
        assert build.get("target") == target, f"{name} builds the wrong stage"


def test_the_runtime_stage_never_copies_tests() -> None:
    dockerfile = Path("Dockerfile").read_text()
    runtime_stage, _, test_stage = dockerfile.partition("FROM runtime AS test")
    assert "COPY tests" not in runtime_stage
    assert "COPY tests" in test_stage


def test_deployment_cannot_be_enabled_from_the_environment() -> None:
    """SPEC forbids production deployment, so it is not a configurable flag."""
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    environment = compose["services"]["api"]["environment"]
    assert environment["ENABLE_PRODUCTION_DEPLOYMENT"] == "false"
    assert "${" not in str(environment["ENABLE_PRODUCTION_DEPLOYMENT"])


def test_execution_and_publication_flags_are_configurable_and_default_off() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    environment = compose["services"]["api"]["environment"]
    for flag in ("ENABLE_CODE_EXECUTION", "ENABLE_GITHUB_PR"):
        assert environment[flag] == f"${{{flag}:-false}}"


def test_office_scripts_declare_no_name_twice() -> None:
    """Every script shares one global scope, so a repeated name silently wins.

    This is how a working screen breaks: two files each declare `modelsView`,
    the file loaded last replaces the other, and one screen calls the wrong
    function.
    """
    import re
    from collections import Counter
    from pathlib import Path

    declaration = re.compile(
        r"^(?:async function|function|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M
    )
    names: Counter[str] = Counter()
    for script in sorted(Path("app/api/web/office").glob("*.js")):
        names.update(set(declaration.findall(script.read_text())))
    repeated = [name for name, count in names.items() if count > 1]
    assert repeated == [], f"declared in more than one script: {repeated}"


def test_tick_boxes_are_not_stretched_to_the_full_row() -> None:
    """The design sizes inputs to 100% width, which hides a radio's label."""
    from pathlib import Path

    css = Path("app/api/web/office.css").read_text()
    assert 'input[type="radio"],' in css and 'input[type="checkbox"] { width: auto;' in css


def test_the_remote_route_is_locked_down_like_the_other_workers() -> None:
    """R15: no published port, no Docker socket and no added privileges was
    already true; it now also drops every capability but the one it needs
    (writing into the exchange folder the office's user owns) and cannot
    gain more."""
    remote = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]["remote"]
    assert "ports" not in remote and "privileged" not in remote
    assert remote.get("cap_drop") == ["ALL"]
    assert remote.get("cap_add") == ["DAC_OVERRIDE"]
    assert "no-new-privileges:true" in remote.get("security_opt", [])
    assert not any("docker.sock" in str(volume) for volume in remote.get("volumes", []))
