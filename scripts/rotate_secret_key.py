"""Rotate the credential-store root key.

Order matters: the new key is written beside the old one, the store is
re-encrypted, and only then does the new key replace the old. A crash at any
point leaves the store readable by root.key or by root.key.new, and rerunning
this command finishes the job.
"""

import os
from pathlib import Path

from app.config import get_settings
from app.secret_store import SecretStore, SecretStoreError, generate_root_key, load_root_key


def rotate(key_file: Path, store_file: Path) -> None:
    pending = key_file.with_name(key_file.name + ".new")
    old_key = load_root_key(key_file)
    if pending.exists():
        new_key = load_root_key(pending)
        try:
            SecretStore(store_file, new_key).verify()
            os.replace(pending, key_file)
            return
        except SecretStoreError:
            pass  # Not yet re-encrypted with the pending key; continue below.
    else:
        generate_root_key(pending)
        new_key = load_root_key(pending)
    SecretStore(store_file, old_key).rotate(new_key)
    os.replace(pending, key_file)


def main() -> None:
    settings = get_settings()
    rotate(settings.secret_key_file, settings.credential_store_path)
    print("Credential root key rotated; every stored credential was re-encrypted.")


if __name__ == "__main__":
    main()
