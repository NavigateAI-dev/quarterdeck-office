"""Root key file creation, standard library only.

Kept apart from the secret store so the launcher and `init_local` can create
the key on a fresh machine before any dependency is installed.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

KEY_BYTES = 32


def generate_root_key(path: Path) -> None:
    """Create a new private 256-bit root key. Never overwrites an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(secrets.token_bytes(KEY_BYTES))
