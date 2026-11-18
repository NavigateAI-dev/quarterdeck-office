"""Ready-made routines, one file each: a new kind needs no code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FOLDER = Path(__file__).parent / "presets"


def listed() -> list[dict[str, Any]]:
    """Each preset: its slug, title, timing and prompt."""
    found = []
    for path in sorted(FOLDER.glob("*.md")):
        _, head, body = path.read_text(encoding="utf-8").split("---\n", 2)
        found.append({"slug": path.stem, **yaml.safe_load(head), "prompt": body.strip()})
    return found
