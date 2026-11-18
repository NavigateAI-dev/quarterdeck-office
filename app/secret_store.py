"""Encrypted local storage for provider and connector credentials.

Secrets are encrypted with AES-GCM under a root key held in a separate file,
outside the database and the store itself. Each secret's name is bound into
its ciphertext as associated data, so an entry cannot be moved to another
name. Losing the root key makes the stored secrets unrecoverable; that is the
intended trade-off, and the recovery path is to re-enter the credentials.
"""

import base64
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.keyfile import KEY_BYTES, generate_root_key

__all__ = ["SecretStore", "SecretStoreError", "generate_root_key", "load_root_key"]

NONCE_BYTES = 12
FORMAT_VERSION = 1
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class SecretStoreError(Exception):
    """A secret-store failure, reported as a code. Never carries a value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def load_root_key(path: Path) -> bytes:
    """Read the root key, refusing one that other users could read."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise SecretStoreError("secret_key_missing") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretStoreError("secret_key_permissions")
    key = path.read_bytes()
    if len(key) != KEY_BYTES:
        raise SecretStoreError("secret_key_invalid")
    return key


class SecretStore:
    """A single encrypted file of named secrets."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise SecretStoreError("secret_key_invalid")
        self.path = path
        self._cipher = AESGCM(key)

    def put(self, name: str, value: str) -> None:
        self._check_name(name)
        if not value:
            raise SecretStoreError("secret_value_empty")
        entries = self._read()
        entries[name] = self._encrypt(name, value)
        self._write(entries)

    def get(self, name: str) -> str:
        self._check_name(name)
        entry = self._read().get(name)
        if entry is None:
            raise SecretStoreError("secret_not_found")
        return self._decrypt(name, entry)

    def exists(self, name: str) -> bool:
        self._check_name(name)
        return name in self._read()

    def names(self, prefix: str = "") -> list[str]:
        """The names held, never the values."""
        return [name for name in self._read() if name.startswith(prefix)]

    def delete(self, name: str) -> bool:
        self._check_name(name)
        entries = self._read()
        if entries.pop(name, None) is None:
            return False
        self._write(entries)
        return True

    def verify(self) -> int:
        """Decrypt every entry; raises if any cannot be read with this key."""
        entries = self._read()
        for name, entry in entries.items():
            self._decrypt(name, entry)
        return len(entries)

    def rotate(self, new_key: bytes) -> "SecretStore":
        """Re-encrypt every secret under a new key in one atomic replacement.

        The caller persists the new key before discarding the old one; until
        then, a failure leaves the store readable with the old key.
        """
        rotated = SecretStore(self.path, new_key)
        entries = self._read()
        rotated._write(
            {
                name: rotated._encrypt(name, self._decrypt(name, entry))
                for name, entry in entries.items()
            }
        )
        return rotated

    @staticmethod
    def _check_name(name: str) -> None:
        if not NAME_PATTERN.match(name):
            raise SecretStoreError("secret_name_invalid")

    @staticmethod
    def _associated_data(name: str) -> bytes:
        return f"quarterdeck-secret:v{FORMAT_VERSION}:{name}".encode()

    def _encrypt(self, name: str, value: str) -> dict[str, str]:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, value.encode(), self._associated_data(name))
        return {
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    def _decrypt(self, name: str, entry: dict[str, str]) -> str:
        try:
            nonce = base64.b64decode(entry["nonce"])
            ciphertext = base64.b64decode(entry["ciphertext"])
            plaintext = self._cipher.decrypt(nonce, ciphertext, self._associated_data(name))
        except (InvalidTag, KeyError, ValueError, TypeError):
            raise SecretStoreError("secret_undecryptable") from None
        return plaintext.decode()

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            data: Any = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise SecretStoreError("secret_store_unreadable") from None
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            raise SecretStoreError("secret_store_unreadable")
        secrets = data.get("secrets")
        if not isinstance(secrets, dict):
            raise SecretStoreError("secret_store_unreadable")
        return secrets

    def _write(self, entries: dict[str, dict[str, str]]) -> None:
        """Replace the file atomically, so a crash never leaves it half-written."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"version": FORMAT_VERSION, "secrets": entries}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
