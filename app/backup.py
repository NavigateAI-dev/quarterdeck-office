"""Local backup and restore.

A backup is one private archive holding a logical dump of every table in the
office's schema (including LangGraph checkpoints, so a pending approval can
resume after restore) and the workspace files, with a manifest of SHA-256
digests. It needs no PostgreSQL client tools.

Deliberately excluded: the credential root key (a backup must not carry the
key to its own encrypted secrets), engineering clones, and runner jobs. The
encrypted credential store is included; without the root key it cannot be
read, so after restoring on a new machine the owner re-enters provider keys or
restores the root key separately.

Restore never overwrites: the target database must be migrated to the same
revision and hold no data, and the target workspace must be empty.

AI history can be sealed under the root key or left out, as the owner chooses.
Sealed tables come back only where that key is; a restore without it brings back
everything else and names what it skipped.
"""

import base64
import hashlib
import io
import json
import os
import tarfile
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Engine, MetaData, func, select, text

FORMAT_VERSION = 1
VERSION_TABLE = "alembic_version"
# The checkpointer's own schema bookkeeping, filled by its setup() rather than
# by the office. Like alembic_version it is versioning, not data: a restore
# target already has it populated, so carrying it would refuse every restore.
CHECKPOINT_VERSION_TABLE = "checkpoint_migrations"
# Schema versioning, not office data: exempt from the "target must be empty"
# check, because a migrated target always has both filled in already.
VERSION_TABLES = frozenset({VERSION_TABLE, CHECKPOINT_VERSION_TABLE})
# Browser sessions are bound to one installation and never travel in a backup.
EXCLUDED_TABLES = VERSION_TABLES | frozenset({"owner_sessions"})
# The owner's conversations from other AI tools, and all that rests on them.
HISTORY_TABLES = frozenset(
    {
        "archive_imports",
        "archive_conversations",
        "archive_messages",
        "archive_memories",
        "archive_readings",
        "archive_access",
    }
)
NONCE_BYTES = 12
EXCLUDED_DIRECTORIES = frozenset({"engineering", "jobs", "backups"})
EXCLUDED_SUFFIXES = (".key", ".tmp")


class BackupError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _encode(value: Any) -> list[Any]:
    """Tag every value so decoding is unambiguous, including JSONB dicts."""
    if value is None or isinstance(value, bool | int | float | str | dict | list):
        return ["n", value]
    if isinstance(value, UUID):
        return ["u", str(value)]
    if isinstance(value, datetime):
        return ["t", value.isoformat()]
    if isinstance(value, date):
        return ["d", value.isoformat()]
    if isinstance(value, Decimal):
        return ["m", str(value)]
    if isinstance(value, bytes | memoryview | bytearray):
        return ["b", base64.b64encode(bytes(value)).decode()]
    raise BackupError(f"unsupported_type:{type(value).__name__}")


def _decode(tagged: list[Any]) -> Any:
    tag, value = tagged
    if tag == "n":
        return value
    if tag == "u":
        return UUID(value)
    if tag == "t":
        return datetime.fromisoformat(value)
    if tag == "d":
        return date.fromisoformat(value)
    if tag == "m":
        return Decimal(value)
    if tag == "b":
        return base64.b64decode(value)
    raise BackupError("unknown_value_tag")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _schema_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        # VERSION_TABLE is a module constant, not input.
        query = text(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608
        return connection.execute(query).scalar()


def _workspace_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.is_symlink():
            continue
        if relative.parts[0] in EXCLUDED_DIRECTORIES or path.name.endswith(EXCLUDED_SUFFIXES):
            continue
        files.append(path)
    return files


def _seal(key: bytes, table: str, data: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, data, table.encode())


def _unseal(key: bytes, table: str, data: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(data[:NONCE_BYTES], data[NONCE_BYTES:], table.encode())
    except InvalidTag:
        raise BackupError("history_key_wrong") from None


def create_backup(
    engine: Engine,
    workspace_root: Path,
    destination: Path,
    history_key: bytes | None = None,
    leave_out_history: bool = False,
) -> Path:
    """With `history_key`, AI history is sealed under it; with `leave_out_history`, it is absent."""
    metadata = MetaData()
    metadata.reflect(bind=engine)
    members: dict[str, bytes] = {}
    tables: dict[str, dict[str, Any]] = {}
    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            if table.name in EXCLUDED_TABLES:
                continue
            if leave_out_history and table.name in HISTORY_TABLES:
                continue
            lines = [
                json.dumps({key: _encode(value) for key, value in row._mapping.items()})
                for row in connection.execute(select(table))
            ]
            data = ("\n".join(lines) + ("\n" if lines else "")).encode()
            name = f"db/{table.name}.jsonl"
            sealed = history_key is not None and table.name in HISTORY_TABLES
            if sealed:
                data = _seal(history_key, table.name, data)  # type: ignore[arg-type]
            members[name] = data
            tables[table.name] = {"rows": len(lines), "sha256": _digest(data), "sealed": sealed}
    files: dict[str, str] = {}
    for path in _workspace_files(workspace_root):
        data = path.read_bytes()
        name = f"workspace/{path.relative_to(workspace_root).as_posix()}"
        members[name] = data
        files[name] = _digest(data)
    manifest = {
        "format": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "schema_revision": _schema_revision(engine),
        "tables": tables,
        "files": files,
    }
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    archive = destination / f"quarterdeck-backup-{stamp}.tar.gz"
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, data in [
            ("manifest.json", json.dumps(manifest, indent=2).encode()),
            *members.items(),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return archive


def _read_verified(archive: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    with tarfile.open(archive, mode="r:gz") as tar:
        contents: dict[str, bytes] = {}
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise BackupError("unsafe_member")
            extracted = tar.extractfile(member)
            contents[member.name] = extracted.read() if extracted else b""
    if "manifest.json" not in contents:
        raise BackupError("manifest_missing")
    manifest = json.loads(contents.pop("manifest.json"))
    if manifest.get("format") != FORMAT_VERSION:
        raise BackupError("unsupported_format")
    expected = {f"db/{name}.jsonl": entry["sha256"] for name, entry in manifest["tables"].items()}
    expected.update(manifest["files"])
    if set(expected) != set(contents):
        raise BackupError("member_set_mismatch")
    for name, data in contents.items():
        if _digest(data) != expected[name]:
            raise BackupError("digest_mismatch")
    return manifest, contents


def restore_backup(
    archive: Path, engine: Engine, workspace_root: Path, history_key: bytes | None = None
) -> list[str]:
    """Returns the sealed tables it could not open, for want of the key."""
    skipped: list[str] = []
    manifest, contents = _read_verified(archive)
    if _schema_revision(engine) != manifest["schema_revision"]:
        raise BackupError("schema_revision_mismatch")
    if workspace_root.exists() and any(workspace_root.iterdir()):
        raise BackupError("workspace_not_empty")
    metadata = MetaData()
    metadata.reflect(bind=engine)
    by_name = {table.name: table for table in metadata.sorted_tables}
    # An archive written before a table joined EXCLUDED_TABLES still carries it;
    # it is skipped below rather than treated as missing from the target.
    missing = set(manifest["tables"]) - set(by_name) - VERSION_TABLES
    if missing:
        raise BackupError(f"table_missing:{sorted(missing)[0]}")
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if table.name in VERSION_TABLES:
                continue
            if connection.execute(select(func.count()).select_from(table)).scalar():
                raise BackupError("target_not_empty")
        for table in metadata.sorted_tables:
            if table.name not in manifest["tables"] or table.name in EXCLUDED_TABLES:
                continue
            data = contents[f"db/{table.name}.jsonl"]
            if manifest["tables"][table.name].get("sealed"):
                if history_key is None:
                    skipped.append(table.name)
                    continue
                data = _unseal(history_key, table.name, data)
            rows = [
                {key: _decode(value) for key, value in json.loads(line).items()}
                for line in data.decode().splitlines()
                if line
            ]
            if rows:
                connection.execute(table.insert(), rows)
        _advance_counters(connection, metadata)
    for name, data in contents.items():
        if not name.startswith("workspace/"):
            continue
        target = workspace_root / name.removeprefix("workspace/")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    return skipped


def _advance_counters(connection: Any, metadata: MetaData) -> None:
    """Move every id counter past the ids just restored.

    Rows come back with the ids they had; a counter left at its start hands
    out an id already taken, and the restored office cannot record its next
    event or message (found on a restored copy).
    """
    quote = connection.dialect.identifier_preparer.quote
    for table in metadata.sorted_tables:
        for column in table.primary_key.columns:
            found = connection.execute(
                text("SELECT pg_get_serial_sequence(:table, :column)"),
                {"table": quote(table.name), "column": column.name},
            ).scalar()
            if not found:
                continue
            # Names come from the reflected schema, quoted; not from input.
            highest = connection.execute(
                text(f"SELECT MAX({quote(column.name)}) FROM {quote(table.name)}")  # noqa: S608
            ).scalar()
            if highest is None:
                continue
            connection.execute(
                text("SELECT setval(:sequence, :value, true)"),
                {"sequence": found, "value": int(highest)},
            )


def rows_match(archived: list[str], restored: list[str]) -> tuple[bool, str]:
    """Whether two tables hold the same rows, in any order and key order.

    Each row is a JSON object as the backup writes it; both sides are put in
    one canonical form and compared as multisets.
    """

    def canonical(lines: list[str]) -> Counter[str]:
        return Counter(
            json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
            for line in lines
            if line.strip()
        )

    want, got = canonical(archived), canonical(restored)
    if want == got:
        return True, f"{sum(got.values())} rows, content matches"
    differing = sum((want - got).values())
    return False, f"{differing} rows differ from the archive"


def verify_restore(
    archive: Path, engine: Engine, workspace_root: Path, history_key: bytes | None = None
) -> list[tuple[str, bool, str]]:
    """Check that a restored office matches the archive it came from.

    A restore that ran without raising is not yet proof: the point of the drill
    is to compare what is now in the database and the workspace against the
    archive, row by row (by content, in any order) and file by file. Returns
    one (name, passed, detail) per check so a caller can print all of them
    rather than stopping at the first failure.
    """
    checks: list[tuple[str, bool, str]] = []
    manifest, contents = _read_verified(archive)

    revision = _schema_revision(engine)
    checks.append(
        (
            "schema revision",
            revision == manifest["schema_revision"],
            f"{revision} (archive: {manifest['schema_revision']})",
        )
    )

    metadata = MetaData()
    metadata.reflect(bind=engine)
    by_name = {table.name: table for table in metadata.sorted_tables}
    with engine.connect() as connection:
        for name, entry in sorted(manifest["tables"].items()):
            if name in EXCLUDED_TABLES:
                # Deliberately not restored, so its row count says nothing.
                continue
            table = by_name.get(name)
            if table is None:
                checks.append((f"table {name}", False, "missing from the restored schema"))
                continue
            sealed = bool(entry.get("sealed"))
            if sealed and history_key is None:
                checks.append((f"table {name}", False, "sealed, and the root key is not here"))
                continue
            count = connection.execute(select(func.count()).select_from(table)).scalar() or 0
            expected = int(entry["rows"])
            if count != expected:
                checks.append((f"table {name}", False, f"{count} rows (archive: {expected})"))
                continue
            # Same count is not the same data: every row is compared, re-encoded
            # exactly as the backup encoded it, in any order.
            restored_rows = [
                json.dumps({key: _encode(value) for key, value in row._mapping.items()})
                for row in connection.execute(select(table))
            ]
            data = contents[f"db/{name}.jsonl"]
            if sealed and history_key is not None:
                data = _unseal(history_key, name, data)
            archived_rows = data.decode().splitlines()
            matched, detail = rows_match(archived_rows, restored_rows)
            checks.append((f"table {name}", matched, detail))

    for name in sorted(manifest["files"]):
        target = workspace_root / name.removeprefix("workspace/")
        if not target.is_file():
            checks.append((f"file {name}", False, "missing from the restored workspace"))
            continue
        digest = _digest(target.read_bytes())
        checks.append((f"file {name}", digest == manifest["files"][name], "content digest"))

    restored = {
        f"workspace/{path.relative_to(workspace_root).as_posix()}"
        for path in _workspace_files(workspace_root)
    }
    extra = sorted(restored - set(manifest["files"]))
    checks.append(
        (
            "no unexpected workspace files",
            not extra,
            f"{len(extra)} unexpected" + (f", first: {extra[0]}" if extra else ""),
        )
    )
    return checks
