from pathlib import Path

from app.secret_store import SecretStore, generate_root_key, load_root_key
from scripts.rotate_secret_key import rotate


def test_rotation_replaces_the_key_and_keeps_every_secret(tmp_path: Path) -> None:
    key_file = tmp_path / "root.key"
    store_file = tmp_path / "credentials.json"
    generate_root_key(key_file)
    old_key = load_root_key(key_file)
    SecretStore(store_file, old_key).put("connection-1", "sk-value-0123456789")

    rotate(key_file, store_file)

    new_key = load_root_key(key_file)
    assert new_key != old_key
    assert SecretStore(store_file, new_key).get("connection-1") == "sk-value-0123456789"
    assert not (tmp_path / "root.key.new").exists()


def test_an_interrupted_rotation_is_resumed_with_the_pending_key(tmp_path: Path) -> None:
    """A crash after re-encryption leaves the new key in root.key.new."""
    key_file = tmp_path / "root.key"
    store_file = tmp_path / "credentials.json"
    generate_root_key(key_file)
    store = SecretStore(store_file, load_root_key(key_file))
    store.put("connection-1", "sk-value-0123456789")
    pending = tmp_path / "root.key.new"
    generate_root_key(pending)
    store.rotate(load_root_key(pending))  # crashed before the key swap

    rotate(key_file, store_file)

    assert SecretStore(store_file, load_root_key(key_file)).get("connection-1") == (
        "sk-value-0123456789"
    )
