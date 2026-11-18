"""A unit of work the run worker executes (split from the worker)."""

from dataclasses import dataclass
from typing import Any

from app.orchestrator.runner import Dispatch


@dataclass
class Job:
    dispatch: Dispatch
    objective: str
    context: dict[str, Any]
    resume: bool = False
    state_override: dict[str, Any] | None = None
    # Which node the graph should continue from. A decision made after the
    # graph reached its end has to re-enter at the node that produced it.
    resume_node: str | None = None
