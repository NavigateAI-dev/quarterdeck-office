"""Resolve a connection's credential reference to its local secret boundary.

References are either ``env:NAME`` (an environment variable of the running
process) or ``secret:NAME`` (an entry in the encrypted secret store). The
value is resolved only at the point of use and never persisted elsewhere.
"""

import os

from app.config import Settings
from app.secret_store import SecretStore, SecretStoreError, load_root_key

ENV_PREFIX = "env:"
SECRET_PREFIX = "secret:"


def open_secret_store(settings: Settings) -> SecretStore:
    """Open the store, loading the root key at call time so a rotated or
    removed key takes effect without a restart."""
    return SecretStore(settings.credential_store_path, load_root_key(settings.secret_key_file))


def secret_name_for_connection(connection_id: object) -> str:
    return f"connection-{connection_id}"


def credential_present(reference: str, settings: Settings) -> bool:
    """True when the referenced secret exists locally. Raises SecretStoreError."""
    if reference.startswith(ENV_PREFIX):
        return bool(os.getenv(reference.removeprefix(ENV_PREFIX)))
    if reference.startswith(SECRET_PREFIX):
        return open_secret_store(settings).exists(reference.removeprefix(SECRET_PREFIX))
    return False


def resolve_credential(reference: str, settings: Settings) -> str:
    """Return the secret value. Raises SecretStoreError with a code, never the value."""
    if reference.startswith(ENV_PREFIX):
        value = os.getenv(reference.removeprefix(ENV_PREFIX))
        if not value:
            raise SecretStoreError("secret_not_found")
        return value
    if reference.startswith(SECRET_PREFIX):
        return open_secret_store(settings).get(reference.removeprefix(SECRET_PREFIX))
    raise SecretStoreError("secret_reference_invalid")
