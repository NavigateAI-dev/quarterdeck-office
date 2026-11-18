"""Curated skills: integrity, no executables, permission review, application."""

import json
from pathlib import Path

import pytest

from app.skills.catalog_store import (
    SkillError,
    SkillManifest,
    expanded_permissions,
    load_catalog,
    load_manifest,
)

CATALOG = Path("app/skills/catalog")


def test_the_shipped_catalog_loads_and_verifies() -> None:
    catalog = load_catalog(CATALOG)
    assert {"source-quality-check", "comparison-table", "competitor-brief"} <= set(catalog)
    assert catalog["competitor-brief"]["1.1.0"].permissions == ["web_search", "local_documents"]


def test_a_tampered_manifest_is_refused(tmp_path: Path) -> None:
    for path in CATALOG.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text())
    target = tmp_path / "comparison-table-1.0.0.json"
    target.write_text(target.read_text().replace("Keep each value", "Invent each value"))
    with pytest.raises(SkillError, match="integrity_mismatch"):
        load_manifest(tmp_path, "comparison-table", "1.0.0")


def base_manifest(**changes: object) -> dict[str, object]:
    manifest = json.loads((CATALOG / "comparison-table-1.0.0.json").read_text())
    return manifest | changes


def test_executable_content_is_refused() -> None:
    with pytest.raises(ValueError, match="executable"):
        SkillManifest.model_validate(base_manifest(executables=["run.sh"]))


def test_unknown_permissions_are_refused() -> None:
    with pytest.raises(ValueError):
        SkillManifest.model_validate(base_manifest(permissions=["send_email"]))


def test_an_unknown_skill_or_version_is_reported() -> None:
    with pytest.raises(SkillError, match="unknown_skill"):
        load_manifest(CATALOG, "nope", "1.0.0")


def test_permission_expansion_is_detected() -> None:
    assert expanded_permissions(["web_search"], ["web_search", "local_documents"]) == [
        "local_documents"
    ]
    assert expanded_permissions(["web_search", "local_documents"], ["web_search"]) == []


def entry_with_skill(role: str, permissions: list[str]) -> dict[str, object]:
    return {
        "profile_id": "p",
        "name": "Researcher",
        "role": role,
        "version": 1,
        "instructions": "",
        "connection_id": None,
        "provider": None,
        "model_id": None,
        "skills": [
            {
                "skill_id": "competitor-brief",
                "version": "1.1.0",
                "name": "Competitor brief",
                "permissions": permissions,
                "instructions": "Cover each competitor's positioning.",
            }
        ],
    }


def test_a_pinned_skill_is_applied_when_its_permissions_are_available() -> None:
    from app.agent_routing import apply_snapshot
    from app.runtime.worker import build_agents
    from tests.unit.test_direct_completion import settings

    agents = build_agents(object(), settings())  # type: ignore[arg-type]
    applied = apply_snapshot(
        agents,
        {"agents": [entry_with_skill("researcher", ["web_search"])]},
        lambda e: None,
        available_permissions={"web_search"},
    )
    assert "Cover each competitor's positioning." in agents.researcher.system_message()
    assert applied == [{"skill_id": "competitor-brief", "version": "1.1.0", "status": "applied"}]


def test_a_skill_needing_an_ungranted_permission_is_skipped_with_a_reason() -> None:
    from app.agent_routing import apply_snapshot
    from app.runtime.worker import build_agents
    from tests.unit.test_direct_completion import settings

    agents = build_agents(object(), settings())  # type: ignore[arg-type]
    applied = apply_snapshot(
        agents,
        {"agents": [entry_with_skill("researcher", ["web_search", "local_documents"])]},
        lambda e: None,
        available_permissions={"web_search"},
    )
    assert "Cover each competitor" not in agents.researcher.system_message()
    assert applied == [
        {
            "skill_id": "competitor-brief",
            "version": "1.1.0",
            "status": "skipped",
            "reason": "missing_permission:local_documents",
        }
    ]
