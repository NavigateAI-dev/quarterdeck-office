"""Agent profile rules shared by the API and run creation."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AgentProfile, AgentProfileVersion, InstalledSkill, ModelConnection

# A connection may be assigned once the provider has confirmed the model.
ASSIGNABLE_CONNECTION_STATUSES = frozenset({"MODEL_AVAILABLE", "VERIFIED"})


class ConnectionNotAssignable(Exception):
    """The connection exists but has not passed a provider test."""


def check_assignable(connection: ModelConnection) -> None:
    if connection.status not in ASSIGNABLE_CONNECTION_STATUSES:
        raise ConnectionNotAssignable(connection.status)


def capture_agent_snapshot(session: Session) -> dict[str, Any]:
    """The enabled version of every active agent, as a run will use it.

    Holds references and plain configuration only, never a credential.
    """
    rows = session.execute(
        select(AgentProfile, AgentProfileVersion, ModelConnection)
        .join(
            AgentProfileVersion,
            (AgentProfileVersion.profile_id == AgentProfile.id)
            & (AgentProfileVersion.version == AgentProfile.enabled_version),
        )
        .outerjoin(ModelConnection, ModelConnection.id == AgentProfileVersion.connection_id)
        .where(AgentProfile.status == "ACTIVE")
        .order_by(AgentProfile.name)
    )
    installed = {
        (row.skill_id, row.version): row
        for row in session.scalars(select(InstalledSkill).where(InstalledSkill.status == "ENABLED"))
    }

    def pinned(version: AgentProfileVersion) -> list[dict[str, Any]]:
        """The pinned skills' content as enabled now, frozen into the run."""
        captured = []
        for pin in version.skills_json or []:
            row = installed.get((pin["skill_id"], pin["version"]))
            if row is None:
                continue  # Uninstalled or disabled since pinning: not applied.
            manifest = row.manifest_json
            captured.append(
                {
                    "skill_id": row.skill_id,
                    "version": row.version,
                    "name": manifest["name"],
                    "permissions": manifest.get("permissions", []),
                    "instructions": manifest["instructions"],
                    "sha256": row.manifest_sha256,
                }
            )
        return captured

    agents = [
        {
            "profile_id": str(profile.id),
            "name": profile.name,
            "role": profile.role,
            "version": version.version,
            "instructions": version.instructions,
            "limits": version.limits_json or {},
            "connection_id": str(version.connection_id) if version.connection_id else None,
            "provider": connection.provider if connection else None,
            "model_id": connection.model_id if connection else None,
            "base_url": connection.base_url if connection else None,
            "skills": pinned(version),
        }
        for profile, version, connection in rows
    ]
    return {"captured_at": datetime.now(UTC).isoformat(), "agents": agents}
