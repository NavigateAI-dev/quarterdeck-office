"""Curated skills: instruction packages with declared permissions.

Skills are instructions and small text resources, never code. Every catalog
manifest is checked against the catalog index's SHA-256 before it can be
installed, a manifest that declares executable content is refused, and the
permissions it may declare are a closed set the application can enforce.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
# Access a skill may require. Checked against what the run actually has.
PERMISSIONS = frozenset({"web_search", "local_documents"})


class SkillError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SkillReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(reviewed|unreviewed)$")
    checks: list[str] = Field(default_factory=list)


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    publisher: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    roles: list[str] = Field(min_length=1, max_length=8)
    permissions: list[str] = Field(default_factory=list, max_length=8)
    # A curated skill is short; one read from the open ecosystem may carry its
    # reference notes too, up to the intake's own limit.
    instructions: str = Field(min_length=1, max_length=24_000)
    changelog: str = ""
    review: SkillReview

    @model_validator(mode="before")
    @classmethod
    def refuse_executables(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(
            key in data for key in ("executables", "scripts", "commands", "entrypoint")
        ):
            raise ValueError("Skills with executable content are not supported")
        return data

    @field_validator("permissions")
    @classmethod
    def known_permissions(cls, value: list[str]) -> list[str]:
        unknown = set(value) - PERMISSIONS
        if unknown:
            raise ValueError(f"Unknown permissions: {sorted(unknown)}")
        return value


def _index(catalog: Path) -> dict[str, dict[str, dict[str, str]]]:
    return dict(json.loads((catalog / "index.json").read_text()))


def load_manifest(catalog: Path, skill_id: str, version: str) -> SkillManifest:
    entry = _index(catalog).get(skill_id, {}).get(version)
    if entry is None:
        raise SkillError("unknown_skill")
    data = (catalog / entry["file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SkillError("integrity_mismatch")
    try:
        manifest = SkillManifest.model_validate_json(data)
    except ValueError:
        raise SkillError("invalid_manifest") from None
    if (manifest.skill_id, manifest.version) != (skill_id, version):
        raise SkillError("invalid_manifest")
    return manifest


def load_catalog(catalog: Path = CATALOG_DIR) -> dict[str, dict[str, SkillManifest]]:
    return {
        skill_id: {version: load_manifest(catalog, skill_id, version) for version in versions}
        for skill_id, versions in _index(catalog).items()
    }


def expanded_permissions(previous: list[str], current: list[str]) -> list[str]:
    return [permission for permission in current if permission not in previous]


def manifest_digest(manifest: SkillManifest) -> str:
    return hashlib.sha256(json.dumps(manifest.model_dump(), sort_keys=True).encode()).hexdigest()
