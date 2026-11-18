import json
import os
from pathlib import Path

import pytest

from app.secret_store import (
    SecretStore,
    SecretStoreError,
    generate_root_key,
    load_root_key,
)

VALUE = "sk-test-provider-key-0123456789"


def store(tmp_path: Path, key: bytes | None = None) -> SecretStore:
    if key is None:
        generate_root_key(tmp_path / "root.key")
        key = load_root_key(tmp_path / "root.key")
    return SecretStore(tmp_path / "secrets.json", key)


def test_a_stored_secret_round_trips(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    secrets.put("connection-1", VALUE)
    assert secrets.get("connection-1") == VALUE


def test_the_value_is_never_written_in_plaintext(tmp_path: Path) -> None:
    store(tmp_path).put("connection-1", VALUE)
    assert VALUE not in (tmp_path / "secrets.json").read_text()


def test_the_store_file_is_private(tmp_path: Path) -> None:
    store(tmp_path).put("connection-1", VALUE)
    assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0


def test_a_different_key_cannot_read_the_store(tmp_path: Path) -> None:
    store(tmp_path).put("connection-1", VALUE)
    other = SecretStore(tmp_path / "secrets.json", os.urandom(32))
    with pytest.raises(SecretStoreError, match="secret_undecryptable"):
        other.get("connection-1")


def test_an_entry_moved_to_another_name_is_rejected(tmp_path: Path) -> None:
    """Names are bound into the ciphertext, so entries cannot be swapped."""
    secrets = store(tmp_path)
    secrets.put("connection-1", VALUE)
    secrets.put("connection-2", "other")
    path = tmp_path / "secrets.json"
    data = json.loads(path.read_text())
    data["secrets"]["connection-2"] = data["secrets"]["connection-1"]
    path.write_text(json.dumps(data))
    with pytest.raises(SecretStoreError, match="secret_undecryptable"):
        secrets.get("connection-2")


def test_a_missing_secret_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="secret_not_found"):
        store(tmp_path).get("absent")


def test_deleting_removes_the_secret(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    secrets.put("connection-1", VALUE)
    assert secrets.delete("connection-1")
    assert not secrets.exists("connection-1")
    assert not secrets.delete("connection-1")


@pytest.mark.parametrize("name", ["", "../escape", "has space", "a" * 200])
def test_invalid_names_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(SecretStoreError, match="secret_name_invalid"):
        store(tmp_path).put(name, VALUE)


def test_an_empty_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="secret_value_empty"):
        store(tmp_path).put("connection-1", "")


def test_the_root_key_is_generated_privately_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "root.key"
    generate_root_key(path)
    first = path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        generate_root_key(path)
    assert path.read_bytes() == first


def test_a_missing_root_key_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="secret_key_missing"):
        load_root_key(tmp_path / "absent.key")


def test_a_malformed_root_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "root.key"
    path.write_bytes(b"short")
    path.chmod(0o600)
    with pytest.raises(SecretStoreError, match="secret_key_invalid"):
        load_root_key(path)


def test_a_readable_root_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "root.key"
    generate_root_key(path)
    path.chmod(0o644)
    with pytest.raises(SecretStoreError, match="secret_key_permissions"):
        load_root_key(path)


def test_verify_detects_a_store_encrypted_under_another_key(tmp_path: Path) -> None:
    store(tmp_path).put("connection-1", VALUE)
    with pytest.raises(SecretStoreError, match="secret_undecryptable"):
        SecretStore(tmp_path / "secrets.json", os.urandom(32)).verify()


def test_rotation_reencrypts_every_secret_under_the_new_key(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    secrets.put("connection-1", VALUE)
    secrets.put("connection-2", "second")
    new_key = os.urandom(32)
    rotated = secrets.rotate(new_key)
    assert rotated.get("connection-1") == VALUE
    assert rotated.get("connection-2") == "second"
    with pytest.raises(SecretStoreError, match="secret_undecryptable"):
        secrets.get("connection-1")
