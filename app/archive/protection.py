"""How AI history is protected: the owner's choices, and the record of who reached it."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ArchiveAccess, ModelConnection, OfficeSettings
from app.office_settings import provider_backups
from app.office_settings import role_models as office_role_models
from app.privacy.detect import masked
from app.secret_store import SecretStoreError, load_root_key

# The office's models from configuration, when a role has no chosen connection.
OFFICE_MODELS = "gateway"
DEFAULTS: dict[str, Any] = {"agents": "discussions", "providers": None, "backups": "encrypted"}
REACH = {
    "discussion": ("discussions", "discussions_and_slack"),
    "slack": ("discussions_and_slack",),
}
RECORD_LIMIT = 30


def choices(session: Session) -> dict[str, Any]:
    row = session.scalar(select(OfficeSettings).limit(1))
    return {**DEFAULTS, **dict(row.archive_protection_json if row else {})}


def backup_choice(session: Session) -> str:
    return str(choices(session)["backups"])


def save(session: Session, chosen: dict[str, Any]) -> None:
    row = session.scalar(select(OfficeSettings).limit(1))
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    row.archive_protection_json = chosen


def root_key(settings: Settings) -> bytes | None:
    try:
        return load_root_key(settings.secret_key_file)
    except SecretStoreError:
        return None


def provider_allowed(session: Session, settings: Settings, agent: dict[str, Any]) -> bool:
    """Whether every provider this agent's words could go to is one the owner ticked.

    The backup counts: when the first provider fails, the same words go there.
    """
    allowed = choices(session)["providers"]
    if allowed is None:
        return True
    entry = agent if agent.get("connection_id") else None
    if entry is None:
        entry = office_role_models(session, settings).get(str(agent.get("role")))
    key = str(entry["connection_id"]) if entry else OFFICE_MODELS
    backup = str((provider_backups(session).get(key) or {}).get("backup") or "")
    return key in allowed and (not backup or backup in allowed)


def may_reach(session: Session, settings: Settings, agent: dict[str, Any], place: str) -> bool:
    return choices(session)["agents"] in REACH[place] and provider_allowed(session, settings, agent)


def note(session: Session, who: str, place: str, action: str, detail: str) -> None:
    session.add(
        ArchiveAccess(who=who[:255], place=place, action=action, detail=masked(detail)[0][:500])
    )


def view(session: Session) -> dict[str, Any]:
    connections = session.scalars(select(ModelConnection).order_by(ModelConnection.display_name))
    record = session.scalars(
        select(ArchiveAccess).order_by(ArchiveAccess.created_at.desc()).limit(RECORD_LIMIT)
    )
    return {
        **choices(session),
        "connections": [
            {"id": OFFICE_MODELS, "name": "The office's configured models"},
            *({"id": str(row.id), "name": row.display_name} for row in connections),
        ],
        "record": [
            {
                "at": row.created_at.isoformat(),
                "who": row.who,
                "place": row.place,
                "action": row.action,
                "detail": row.detail,
            }
            for row in record
        ],
    }
