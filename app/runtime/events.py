"""Facts the run engine announces on the office's event bus.

The engine never imports who listens: Slack notifications, the Desk and plugins
subscribe to these instead of being called.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RunSettled:
    """A run stopped executing here: finished, failed, or waiting for the owner."""

    run_id: UUID
    status: str
