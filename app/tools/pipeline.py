"""The one way a tool is found, checked and run.

A tool is a `ToolSpec`: a name, what it is for, the arguments it takes, and
how it runs. `ToolPipeline.call` finds the named tool, checks the arguments
against its input model, runs it within its timeout, and turns the office's
refusals into an outcome the caller decides on:

- **refused**: the owner's stance, a policy or the server said no — the call
  costs only itself;
- **ask**: a policy wants the owner's word; a task stops for it, a discussion
  proposes it as a button. The caller raises or proposes `outcome.error`;
- **invalid**: the arguments do not fit, named field by field so a model can
  correct them; the tool never runs;
- **failed**: the tool timed out.

Anything else a tool raises is not the pipeline's to hide: each caller keeps its
own catch and treats it as one failed call (the lead's loop, room replies, a
team run's tool round, Slack lookups), so a page that will not load or a
server that is down never costs the answer.

The owner's stance, the office's policies and the price of a third party's tool
are checked inside `McpTools.call`, before its transport, as they were.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.mcp.calling import ApprovalRequired, ToolRefused
from app.policies.store import PolicyAsk, PolicyRefused

Status = Literal["ok", "refused", "ask", "invalid", "failed"]


@dataclass(frozen=True)
class ToolResult:
    """What a tool returned: text, and a picture or source address where it has one."""

    text: str
    picture: bytes | None = None
    source: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """One tool the office can run.

    `input_model` checks the arguments; a tool without one (a third party's,
    whose server checks its own) gets them as given, described by
    `schema`. `concurrency_safe` and `side_effect` say how the lead loop may
    schedule it and whether it needs the side-effect ledger.
    """

    name: str
    description: str
    run: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    input_model: type[BaseModel] | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None
    concurrency_safe: bool = False
    side_effect: bool = False

    @property
    def input_schema(self) -> dict[str, Any]:
        """The arguments as JSON schema, for the model that chooses the call."""
        if self.input_model is not None:
            return self.input_model.model_json_schema()
        return self.schema


@dataclass(frozen=True)
class ToolOutcome:
    status: Status
    text: str = ""
    reason: str = ""
    picture: bytes | None = None
    source: str = ""
    error: Exception | None = None


class ToolPipeline:
    """The tools one caller may use, and the checks in front of them."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"two tools are named {spec.name!r}")
            self._specs[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        spec = self._specs.get(name)
        if spec is None:
            return ToolOutcome("refused", reason="tool_not_offered")
        if spec.input_model is not None:
            try:
                checked = spec.input_model.model_validate(arguments)
            except ValidationError as invalid:
                return ToolOutcome("invalid", reason=_where(invalid))
            arguments = checked.model_dump()
        try:
            async with asyncio.timeout(spec.timeout_s):
                result = await spec.run(arguments)
        except TimeoutError:
            return ToolOutcome("failed", reason="tool_timeout")
        except PolicyAsk as asked:
            return ToolOutcome("ask", reason="owner_decides", error=asked)
        except ToolRefused as refused:
            return ToolOutcome("refused", reason=refused.reason, error=refused)
        except ApprovalRequired as approval:
            return ToolOutcome("refused", reason="approval_required", error=approval)
        except PolicyRefused as policy:
            return ToolOutcome("refused", reason="policy_refused", error=policy)
        return ToolOutcome("ok", text=result.text, picture=result.picture, source=result.source)


def _where(invalid: ValidationError) -> str:
    """Each problem with the arguments, named by its field: `query: Field required`."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '(arguments)'}: {error['msg']}"
        for error in invalid.errors()
    )
