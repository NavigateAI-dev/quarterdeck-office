"""Resolve a snapshotted agent connection into a completer, at call time."""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.credentials import resolve_credential
from app.db.models import ModelConnection
from app.gateway.direct import Completer, DirectCompleter, UnavailableCompleter
from app.secret_store import SecretStoreError


def completer_for_entry(
    entry: dict[str, Any],
    settings: Settings,
    session_factory: sessionmaker[Session] | None,
) -> Completer:
    """The snapshot holds the connection's identity, provider and model; the
    key is read from the local secret boundary only now, and never stored
    with the run. A missing key fails the call that needs it.
    """
    label = f"connection:{entry['connection_id']}"
    reference = None
    base_url = entry.get("base_url")
    if session_factory is not None:
        with session_factory() as session:
            connection = session.get(ModelConnection, UUID(entry["connection_id"]))
            reference = connection.credential_ref if connection is not None else None
            base_url = connection.base_url if connection is not None else base_url
    if not reference:
        return UnavailableCompleter(label, "credential_unavailable")
    try:
        credential = resolve_credential(reference, settings)
    except SecretStoreError:
        return UnavailableCompleter(label, "credential_unavailable")
    return DirectCompleter(
        entry["provider"], entry["model_id"], credential, settings, label=label, base_url=base_url
    )
