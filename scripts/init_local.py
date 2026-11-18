from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.keyfile import generate_root_key


def create_root_key(root: Path) -> None:
    """Create the credential-store root key once; never replace an existing one."""
    key = root / ".secrets" / "root.key"
    key.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        generate_root_key(key)
    except FileExistsError:
        print("Credential root key already exists; kept.")
        return
    print(
        f"Created credential root key at {key.relative_to(root)}. Losing it means re-entering keys."
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    create_root_key(root)
    content = (root / ".env.example").read_text()
    content = content.replace("replace-with-random-local-password", secrets.token_hex(24))
    content = content.replace("replace-with-random-owner-token", secrets.token_urlsafe(32))
    try:
        descriptor = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(".env already exists; no changes made.")
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    print(
        "Created private .env with local service credentials. Configure provider models and keys."
    )


if __name__ == "__main__":
    main()
