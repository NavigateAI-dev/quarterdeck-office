"""An agent as a file: exported to be read, kept or restored.

The file carries what the owner decided — the role, the instructions, the
model by name, the skills by version, the limits — and nothing the office
alone knows: no keys, no identifiers from this installation, no history. An
import is therefore a proposal that the office checks, not a state to trust.
"""

from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AgentProfile, AgentProfileVersion, InstalledSkill, ModelConnection

FORMAT = 1


def export_agent(session: Session, profile: AgentProfile) -> dict[str, Any]:
    """The agent's enabled version, in words another office could read."""
    version = session.scalar(
        select(AgentProfileVersion).where(
            AgentProfileVersion.profile_id == profile.id,
            AgentProfileVersion.version == profile.enabled_version,
        )
    )
    connection = (
        session.get(ModelConnection, version.connection_id)
        if version and version.connection_id
        else None
    )
    return {
        "format": FORMAT,
        "name": profile.name,
        "role": profile.role,
        "instructions": (version.instructions if version else "") or "",
        # By provider and model, not by this office's connection id.
        "model": (
            {"provider": connection.provider, "model": connection.model_id} if connection else None
        ),
        "skills": [
            {"skill_id": pin["skill_id"], "version": pin["version"]}
            for pin in ((version.skills_json if version else None) or [])
        ],
        "limits": (version.limits_json if version else None) or {},
    }


def to_yaml(document: dict[str, Any]) -> str:
    return str(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def read_yaml(text: str) -> dict[str, Any]:
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError("not_an_agent")
    if int(document.get("format", 0)) != FORMAT:
        raise ValueError("unsupported_format")
    if not str(document.get("name", "")).strip() or not str(document.get("role", "")).strip():
        raise ValueError("name_and_role_required")
    return document


def plan_import(session: Session, document: dict[str, Any]) -> dict[str, Any]:
    """What the office would do with this file, and what it cannot honour.

    An import never overwrites an agent and never grants a skill the office has
    not installed and approved: those are reported instead.
    """
    name = str(document["name"]).strip()
    taken = session.scalar(select(AgentProfile).where(AgentProfile.name == name)) is not None
    wanted = document.get("model") or {}
    connection = None
    if wanted:
        connection = session.scalar(
            select(ModelConnection).where(
                ModelConnection.provider == str(wanted.get("provider", "")),
                ModelConnection.model_id == str(wanted.get("model", "")),
            )
        )
    skills: list[dict[str, str]] = []
    missing_skills: list[str] = []
    for pin in document.get("skills") or []:
        row = session.scalar(
            select(InstalledSkill).where(
                InstalledSkill.skill_id == str(pin.get("skill_id")),
                InstalledSkill.version == str(pin.get("version")),
                InstalledSkill.status == "ENABLED",
            )
        )
        if row is None:
            missing_skills.append(f"{pin.get('skill_id')} {pin.get('version')}")
        else:
            skills.append({"skill_id": row.skill_id, "version": row.version})
    notes = []
    if taken:
        notes.append(f"An agent called “{name}” already exists; choose another name.")
    if wanted and connection is None:
        notes.append(
            f"No model connection for {wanted.get('provider')} {wanted.get('model')};"
            " the agent will use the office's model."
        )
    if missing_skills:
        notes.append(
            "These skills are not installed and enabled here, so they are left out: "
            + ", ".join(missing_skills)
        )
    return {
        "name": name,
        "role": str(document["role"]).strip(),
        "instructions": str(document.get("instructions") or ""),
        "connection_id": str(connection.id) if connection else None,
        "skills": skills,
        "limits": dict(document.get("limits") or {}),
        "name_taken": taken,
        "notes": notes,
        "can_import": not taken,
    }
