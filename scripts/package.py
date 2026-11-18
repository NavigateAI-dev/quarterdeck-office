"""Build a release archive: the office as one file a second machine can open.

    python3 -m scripts.package [--out DIR]

What this is and is not. It is the thing the distribution plan calls the first
step — a guided launcher over Compose, packaged so a person receives one file
instead of a repository. It is **not** a signed native installer: that needs an
Apple Developer ID and is a separate milestone. The archive says so itself, so
nobody receiving it believes it has been signed.

What goes in is what a machine with Docker needs to start the office and
nothing else: the source, the Compose files, the migrations, the scripts and
the papers. What stays out is everything private or
rebuildable — `.env`, `.secrets`, backups, the workspace, the sources folder,
caches, and the virtual environment. That list is asserted by a test rather
than trusted, because the failure it prevents is shipping the owner's keys.

Only the standard library, like the launcher: this must run before anything is
installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# What a second machine needs. Directories are taken whole, minus EXCLUDED.
INCLUDE = (
    "app",
    "migrations",
    "scripts",
    "docs",
    "alembic.ini",
    # The template `init_local` reads on first start. It holds no real values;
    # without it a second machine cannot create its own configuration at all.
    ".env.example",
    "docker-compose.yml",
    "docker-compose.drill.yml",
    "Dockerfile",
    # The remote route's own image; the default stack builds it.
    "remote",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "ARCHITECTURE.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "LICENSE",
    "NOTICE",
)

# Never packaged, whatever it is nested in. Keys and private data first: a
# release archive is the easiest way to hand someone a secret by accident.
EXCLUDED_NAMES = frozenset(
    {
        ".env",
        ".secrets",
        "root.key",
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".DS_Store",
    }
)
# The owner's own data, excluded where it lives: at the top of the office's
# folder. Matched anywhere, "sources" also dropped the office's own
# `app/sources` package, and a release could not start.
OWNER_FOLDERS = frozenset({"backups", "workspace", "sources", "sources-exchange"})
EXCLUDED_SUFFIXES = (".pyc", ".key", ".pem", ".sqlite", ".log")

NOT_SIGNED = (
    "This archive is not signed and not notarised. macOS will warn about it, "
    "and that warning is correct: check the SHA-256 below against the one you "
    "were given out of band before you trust it."
)


def wanted(path: Path) -> bool:
    """Whether one file belongs in the archive."""
    if path.name == ".env.example":
        return True
    if any(part in EXCLUDED_NAMES for part in path.parts):
        return False
    if path.parts and path.parts[0] in OWNER_FOLDERS:
        return False
    return path.suffix not in EXCLUDED_SUFFIXES


def members(root: Path = ROOT) -> list[Path]:
    """Every file to package, relative to the repository root, sorted."""
    found: list[Path] = []
    for name in INCLUDE:
        source = root / name
        if not source.exists():
            continue
        if source.is_file():
            if wanted(Path(name)):
                found.append(Path(name))
            continue
        for path in source.rglob("*"):
            if path.is_file():
                relative = path.relative_to(root)
                if wanted(relative):
                    found.append(relative)
    return sorted(set(found))


def manifest(version: str, files: list[Path]) -> dict[str, object]:
    return {
        "name": "quarterdeck",
        "version": version,
        "built_at": datetime.now(UTC).isoformat(),
        "signed": False,
        "note": NOT_SIGNED,
        "files": len(files),
        "start": "python3 -m scripts.launch",
    }


def build(out_dir: Path, version: str, root: Path = ROOT) -> Path:
    """Write the archive and its checksum. Returns the archive's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = members(root)
    if not files:
        raise SystemExit("nothing to package: run this from the office's own folder")
    archive = out_dir / f"quarterdeck-{version}.tar.gz"
    stem = f"quarterdeck-{version}"
    with tarfile.open(archive, "w:gz") as bundle:
        for relative in files:
            bundle.add(root / relative, arcname=f"{stem}/{relative.as_posix()}")
        written = manifest(version, files)
        _add_text(bundle, f"{stem}/RELEASE.json", json.dumps(written, indent=2) + "\n")
        _add_text(bundle, f"{stem}/INSTALL.txt", install_note(version))
    digest = sha256_of(archive)
    (out_dir / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


def install_note(version: str) -> str:
    return f"""Quarterdeck {version}

To start it on this machine:

  1. Install Docker Desktop and make sure it is running.
  2. Open a terminal in this folder.
  3. Run:  python3 -m scripts.launch

The first start builds the images and takes a few minutes. It then creates
private local configuration, starts the office, and opens your browser signed
in. No account is needed and nothing is sent anywhere by starting it.

Your keys are entered in the office itself and stored encrypted on this
machine. They are not in this archive.

{NOT_SIGNED}
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _add_text(bundle: tarfile.TarFile, name: str, body: str) -> None:
    import io

    raw = body.encode()
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    info.mtime = int(datetime.now(UTC).timestamp())
    bundle.addfile(info, io.BytesIO(raw))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "dist"), help="where to write the archive")
    arguments = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from app.version import VERSION

    archive = build(Path(arguments.out), VERSION)
    print(f"ok   {archive}")
    print(f"ok   {archive.name}.sha256")
    print(f"     {sha256_of(archive)}")
    print("note this archive is not signed or notarised; signing is a separate milestone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
