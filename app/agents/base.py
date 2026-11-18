import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.context import ProjectContext
from app.events import InvocationEvent, InvocationRecorder
from app.gateway.client import GatewayError, strip_code_fence
from app.gateway.direct import Completer
from app.policies.store import PolicyAsk

OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentOutputError(Exception):
    """The model response cannot satisfy the role's output contract."""

    def __init__(self, role: str, code: str) -> None:
        self.role = role
        self.code = code
        super().__init__(f"{role}:{code}")


@dataclass(frozen=True)
class Preparation:
    """Per-invocation tool material. Never stored on the agent instance."""

    prompt_section: str = ""
    allowed_sources: dict[str, str] = field(default_factory=dict)
    search_results: int | None = None


# How much of the reasons a call records: enough to tell what went wrong.
DETAIL_CHARS = 1000
REASONS_SHOWN = 4


def what_was_wrong(exc: Exception) -> str:
    """Why an output failed its schema, in words: never the output itself.

    Pydantic's messages are read without the input values, so nothing the
    model wrote is carried into the record or the next request.
    """
    if isinstance(exc, json.JSONDecodeError):
        if exc.pos >= len(exc.doc.rstrip()) - 1:
            return "not valid JSON: it ends before the object is complete"
        return f"not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
    if isinstance(exc, ValidationError):
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or 'the object'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        more = len(problems) - REASONS_SHOWN
        listed = "; ".join(problems[:REASONS_SHOWN])
        return listed + (f"; and {more} more" if more > 0 else "")
    return "not a JSON object of the expected shape"


class BaseAgent(Generic[OutputT]):
    output_schema: type[OutputT]
    role: str
    model_alias: str
    instructions: str
    # Which path serves the role: "office" is the role's chosen model by alias;
    # set to the provider by a run snapshot or the role's chosen connection.
    provider: str = "office"
    # A per-role output cap; None uses the configured default.
    max_output_tokens: int | None = None
    # Read after the agent's own preparation, before it writes.
    late_inputs: Callable[[], str] | None = None

    def system_message(self) -> str:
        """Instructions plus the literal output schema.

        The schema is sent on every request. A model cannot match a contract it
        has never been shown, and models otherwise return well-formed JSON in a
        shape of their own invention.
        """
        schema = json.dumps(self.output_schema.model_json_schema(), separators=(",", ":"))
        return (
            f"{self.instructions}\n\n"
            "Return a single JSON object and nothing else. No Markdown, no code "
            "fence, no commentary. It must validate against this JSON Schema, "
            "using exactly these property names and no additional properties:\n"
            f"{schema}"
        )

    def __init__(
        self,
        gateway: Completer,
        recorder: InvocationRecorder | None = None,
        attempts: int = 2,
    ) -> None:
        # Replaced by a direct completer when the run snapshot assigns a connection.
        self.gateway: Completer = gateway
        self.recorder = recorder if recorder is not None else InvocationRecorder()
        self.attempts = attempts

    async def prepare(
        self, objective: str, context: ProjectContext, focus: str = ""
    ) -> Preparation:
        """Gather tool material before invocation.

        `focus` names an evidence gap to close on a rerun. Roles without tools
        use the default.
        """
        return Preparation()

    def validate_output(self, output: OutputT, preparation: Preparation) -> None:
        """Reject output that tool material does not support. Default accepts."""

    async def run(
        self,
        objective: str,
        context: ProjectContext,
        inputs: str = "",
        focus: str = "",
    ) -> OutputT:
        """Invoke the role.

        `inputs` carries upstream artifacts; `focus` names an evidence gap that
        a rerun should close.
        """
        started = monotonic()
        try:
            preparation = await self.prepare(objective, context, focus)
        except PolicyAsk:
            # A question for the owner is not a failed tool: the task pauses
            # where it stands and continues once they have answered.
            raise
        except Exception as exc:
            code = getattr(exc, "code", "tool_failed")
            self._emit(context, "error", monotonic() - started, error_code=str(code))
            raise AgentOutputError(self.role, str(code)) from None

        # What arrived while the agent was reading: its teammates' posts,
        # read now rather than when the step started, so a peer's finding from
        # the same wave is in front of it before it writes.
        late = self.late_inputs() if callable(self.late_inputs) else ""
        messages = [
            {"role": "system", "content": self.system_message()},
            {
                "role": "user",
                "content": (
                    f"{context.render()}\n\nObjective:\n{objective}"
                    f"{preparation.prompt_section}"
                    f"{chr(10) + chr(10) + inputs if inputs else ''}"
                    f"{chr(10) + chr(10) + late if late else ''}"
                ),
            },
        ]
        return await self._complete_validated(messages, context, preparation, started)

    async def _complete_validated(
        self,
        messages: list[dict[str, str]],
        context: ProjectContext,
        preparation: Preparation,
        started: float,
    ) -> OutputT:
        """Request output and validate it, allowing bounded corrective retries.

        Models intermittently return well-formed-looking JSON that does not
        satisfy the contract. One corrective attempt recovers most of these;
        invalid output never advances the workflow.
        """
        attempt = 0
        spent: list[Any] = []
        # Every retry goes back to the original request with a note of what was
        # wrong. Resending the failed answer made each retry larger than the
        # last: one of the owner's steps paid 52k, 65k, then 79k prompt tokens.
        original = list(messages)
        # Why each failed attempt failed, kept with the call's record.
        reasons: list[str] = []
        while True:
            attempt += 1
            try:
                if self.max_output_tokens is None:
                    completion = await self.gateway.complete(
                        self.model_alias, messages, schema=self.output_schema
                    )
                else:
                    completion = await self.gateway.complete(
                        self.model_alias,
                        messages,
                        max_tokens=self.max_output_tokens,
                        schema=self.output_schema,
                    )
                spent.append(completion)
            except GatewayError as exc:
                if exc.code == "incomplete_response":
                    reasons.append(f"attempt {attempt}: cut off at the output limit")
                if exc.code == "incomplete_response" and attempt < self.attempts:
                    # The answer was cut off at the output cap, not refused.
                    # Asking again for a shorter one recovers the deliverable;
                    # failing the run here throws away every step before it.
                    messages = [
                        *original,
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was cut off before it ended, so it "
                                "could not be used. Answer again, complete and shorter: keep "
                                "every required property, keep the substance, and cut length "
                                "by writing fewer and tighter items rather than dropping "
                                "fields. Return the whole JSON object."
                            ),
                        },
                    ]
                    continue
                self._emit(
                    context,
                    "error",
                    monotonic() - started,
                    error_code=exc.code,
                    search_results=preparation.search_results,
                    attempts=attempt,
                    spent=spent,
                    reasons=reasons,
                )
                raise AgentOutputError(self.role, exc.code) from None
            try:
                payload: Any = json.loads(strip_code_fence(completion.content))
                output = self.output_schema.model_validate(payload)
                self.validate_output(output, preparation)
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                wrong = what_was_wrong(exc)
                reasons.append(f"attempt {attempt}: {wrong}")
                if attempt >= self.attempts:
                    self._emit(
                        context,
                        "rejected",
                        monotonic() - started,
                        error_code="malformed_structured_output",
                        completion=completion,
                        search_results=preparation.search_results,
                        attempts=attempt,
                        spent=spent,
                        reasons=reasons,
                    )
                    raise AgentOutputError(self.role, "malformed_structured_output") from None
                messages = [
                    *original,
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response did not satisfy the schema: {wrong}. "
                            "Answer the request above again as one complete JSON object, "
                            "with a value for every required property and no commentary. "
                            "If it was long, write fewer and tighter items rather than "
                            "dropping fields."
                        ),
                    },
                ]
                continue
            except AgentOutputError as exc:
                # A provenance failure is correctable: the model may cite only
                # the supplied sources, and a retry cannot weaken that, because
                # acceptance still requires every source to be in the set.
                reasons.append(f"attempt {attempt}: {exc.code}")
                if attempt >= self.attempts:
                    self._emit(
                        context,
                        "rejected",
                        monotonic() - started,
                        error_code=exc.code,
                        completion=completion,
                        search_results=preparation.search_results,
                        attempts=attempt,
                        spent=spent,
                        reasons=reasons,
                    )
                    raise
                messages = [
                    *original,
                    {
                        "role": "user",
                        "content": (
                            f"That response was rejected ({exc.code}). Every source must be "
                            "one of the supplied sources, copied exactly, with its stated "
                            "source type. Return the complete JSON object again."
                        ),
                    },
                ]
                continue
            self._emit(
                context,
                "ok",
                monotonic() - started,
                completion=completion,
                search_results=preparation.search_results,
                attempts=attempt,
                spent=spent,
                reasons=reasons,
            )
            return output

        self._emit(
            context,
            "ok",
            completion.latency_seconds,
            completion=completion,
            search_results=preparation.search_results,
        )
        return output

    def _emit(
        self,
        context: ProjectContext,
        status: str,
        latency: float,
        *,
        error_code: str | None = None,
        completion: Any = None,
        search_results: int | None = None,
        attempts: int = 1,
        spent: list[Any] | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        attempts_made = spent if spent is not None else ([completion] if completion else [])

        def total(values: list[Any]) -> Any:
            known = [value for value in values if value is not None]
            return sum(known) if known else None

        self.recorder.record(
            InvocationEvent(
                started_at=datetime.now(UTC) - timedelta(seconds=max(latency, 0.0)),
                role=self.role,
                model_alias=self.model_alias,
                resolved_model=completion.model if completion else None,
                project_id=context.project_id,
                status=status,
                latency_seconds=max(latency, 0.0),
                provider=self.provider,
                prompt_tokens=total([c.usage.prompt_tokens for c in attempts_made]),
                completion_tokens=total([c.usage.completion_tokens for c in attempts_made]),
                estimated_cost=total([c.estimated_cost for c in attempts_made]),
                error_code=error_code,
                error_detail="; ".join(reasons)[:DETAIL_CHARS] if reasons else None,
                search_results=search_results,
                attempts=attempts,
            )
        )
