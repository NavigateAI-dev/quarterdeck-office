import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("quarterdeck.invocation")


class InvocationEvent(BaseModel):
    """Structured record of one agent invocation.

    Prompts, model output, and credentials are deliberately excluded so events
    are safe to log and persist.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID | None = None
    task_id: UUID | None = None
    role: str = Field(min_length=1, max_length=64)
    # "office" for the role's chosen model by alias, otherwise the direct provider
    # ("litellm" on calls made through the gateway before 22 Oct 2026).
    provider: str = Field(default="office", max_length=32)
    model_alias: str = Field(min_length=1, max_length=64)
    resolved_model: str | None = Field(default=None, max_length=128)
    project_id: UUID
    status: str = Field(min_length=1, max_length=32)
    latency_seconds: float = Field(default=0.0, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    # Why attempts were rejected, per attempt: never the output itself.
    error_detail: str | None = Field(default=None, max_length=1000)
    search_results: int | None = Field(default=None, ge=0)
    # Token and cost totals cover every attempt, including corrective retries.
    attempts: int = Field(default=1, ge=1)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InvocationRecorder:
    """Collects invocation events and logs them as structured JSON lines."""

    def __init__(
        self,
        run_id: UUID | None = None,
        task_id: UUID | None = None,
        sink: Callable[[InvocationEvent], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.sink = sink
        self.events: list[InvocationEvent] = []

    def record(self, event: InvocationEvent) -> None:
        if self.run_id is not None or self.task_id is not None:
            event = event.model_copy(update={"run_id": self.run_id, "task_id": self.task_id})
        self.events.append(event)
        logger.info(event.model_dump_json())
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception:
                # Losing a usage row must not fail the agent call it describes.
                logger.exception('{"event":"usage_not_persisted"}')
