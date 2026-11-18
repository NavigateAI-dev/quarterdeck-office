"""Where a coding task's progress goes while the coder on the Mac works, and
the owner's Stop and comments that come back."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BuildObserver:
    step: Callable[[dict[str, Any]], None]
    stop: Callable[[], bool]
    # The owner's comments waiting for the coder, and marking them delivered.
    notes: Callable[[], list[dict[str, Any]]] = field(default=lambda: [])
    take: Callable[[list[str]], None] = field(default=lambda ids: None)
