"""Choose the folder Quarterdeck may read, with the native macOS folder dialog.

    python3 -m scripts.pick_folder

The chosen folder is mounted read-only into the no-network `sources` service
and nowhere else. Broad or sensitive folders are refused: the whole disk, your
entire home folder, and credential stores. Standard library only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "LOCAL_SOURCES_DIR"
SENSITIVE_PARTS = {".ssh", ".gnupg", ".aws", ".config", "Keychains", ".docker", ".kube"}
PROMPT = "Choose the folder Quarterdeck may read (read-only)"


def refusal(path: Path, home: Path) -> str | None:
    """Why a folder may not be shared, or None when it may."""
    if path == Path("/"):
        return "whole_disk"
    if path == home:
        return "entire_home_folder"
    if SENSITIVE_PARTS & set(path.parts):
        return "credential_folder"
    return None


def set_env_value(env_file: Path, name: str, value: str) -> None:
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[index] = f"{name}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{name}={value}")
    temporary = env_file.with_name(".env.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    os.replace(temporary, env_file)


def choose() -> Path | None:
    script = f'POSIX path of (choose folder with prompt "{PROMPT}")'
    result = subprocess.run(  # noqa: S603 - fixed argument vector
        ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None  # Cancelled.
    return Path(result.stdout.strip()).resolve()


def main() -> int:
    if sys.platform != "darwin":
        print(f"The folder dialog is macOS only. Set {KEY} in .env to a folder path instead.")
        return 1
    folder = choose()
    if folder is None:
        print("No folder chosen; nothing changed.")
        return 0
    reason = refusal(folder, Path.home().resolve())
    if reason:
        print(f"Refused ({reason}): choose a narrower folder, such as a project folder.")
        return 1
    set_env_value(ROOT / ".env", KEY, str(folder))
    print(f"Shared read-only: {folder}")
    remount = subprocess.run(  # noqa: S603
        ["docker", "compose", "up", "-d", "sources"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if remount.returncode != 0:
        print("Saved. Start the office (python3 -m scripts.launch) to apply it.")
    else:
        print("Applied. Add its subfolders under Knowledge in the browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
