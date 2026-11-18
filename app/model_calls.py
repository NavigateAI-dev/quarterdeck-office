"""Every model call a run makes, kept so any step can be replayed; and the calls
behind discussion replies and Slack, so a reply can be read back the same way.

What a model saw is what the office keeps: each call's request (messages,
tools, the output schema asked for, the limits) and its answer, against the run
and role that made it. Bodies are kept once, by the digest of their content
(`model_payloads`): a lead run sends its whole conversation again on every
call, so each message is stored the first time and named after that.

With a run's calls kept, `ReplayCompleter` answers each request from them, so a
run can be played again without a gateway or an API key, and a request that
differs from the one recorded is refused instead of being answered by a model.

`KeptCompleter` sits inside the policy and budget layers, around the model and
its backups: only calls that reach a model are kept, as the model got them. A
failure to keep one is logged and never fails the call.

A call behind a discussion reply names the owner's message it answered and goes
when that message does. One behind Slack names its thread and is let go after
`SLACK_KEPT_FOR`, as Slack's own words are not the office's to keep for long.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ModelCall, ModelPayload, RoomMessage
from app.gateway.client import Completion, GatewayError

logger = logging.getLogger(__name__)

OK = "ok"
NOT_RECORDED = "not_recorded"
SLACK_KEPT_FOR = timedelta(days=30)


def digest(value: Any) -> str:
    """The content's name: the same content is always the same name."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def schema_of(schema: Any) -> Any:
    """The output schema asked for, as content: a model class is its JSON schema."""
    if schema is None:
        return None
    to_json = getattr(schema, "model_json_schema", None)
    return to_json() if callable(to_json) else schema


def request_of(
    model_alias: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    schema: Any,
    tools: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A request as it is kept (bodies named by digest), and the bodies by name."""
    bodies: dict[str, Any] = {}

    def named(value: Any) -> str | None:
        if value is None:
            return None
        name = digest(value)
        bodies[name] = value
        return name

    shaped = {
        "model_alias": model_alias,
        "messages": [named(message) for message in messages],
        "max_tokens": max_tokens,
        "schema": named(schema_of(schema)),
        "tools": named(tools) if tools else None,
    }
    return shaped, bodies


class ModelCalls:
    """The store: a run's calls, and the bodies they name."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def keep(
        self,
        run_id: UUID | None,
        role: str,
        request: dict[str, Any],
        bodies: dict[str, Any],
        completion: Completion | None,
        status: str,
        latency: float,
        *,
        topic_id: UUID | None = None,
        message_id: int | None = None,
        chat_ref: str | None = None,
    ) -> int:
        answer = completion.model_dump(mode="json") if completion is not None else None
        if answer is not None:
            bodies = {**bodies, digest(answer): answer}
        with self.session_factory() as session, session.begin():
            for name, body in bodies.items():
                session.execute(
                    insert(ModelPayload)
                    .values(digest=name, body=body, size=len(json.dumps(body, default=str)))
                    .on_conflict_do_nothing(index_elements=["digest"])
                )
            origin = "task" if run_id else "room" if topic_id else "slack"
            if origin == "slack":
                # Slack's words are let go after a while; so are the calls behind them.
                session.execute(
                    delete(ModelCall).where(
                        ModelCall.origin == "slack",
                        ModelCall.created_at < datetime.now(UTC) - SLACK_KEPT_FOR,
                    )
                )
            call = ModelCall(
                run_id=run_id,
                origin=origin,
                topic_id=topic_id,
                message_id=message_id,
                chat_ref=chat_ref[:160] if chat_ref else None,
                role=role[:32],
                model_alias=str(request.get("model_alias") or "")[:128],
                model=(completion.model[:255] if completion is not None else None),
                request_digest=digest(request),
                request_json=request,
                response_digest=digest(answer) if answer is not None else None,
                status=status[:64],
                prompt_tokens=(completion.usage.prompt_tokens if completion is not None else None),
                completion_tokens=(
                    completion.usage.completion_tokens if completion is not None else None
                ),
                latency_seconds=latency,
            )
            session.add(call)
            session.flush()
            return int(call.id)

    def of_run(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.id)
            )
            return [call_view(row) for row in rows]

    def of_reply(self, topic_id: UUID, reply_id: int) -> list[dict[str, Any]] | None:
        """The calls behind an agent's reply in a discussion: those made answering
        the message it replies to, by its role. None when there is no such message."""
        with self.session_factory() as session:
            reply = session.get(RoomMessage, reply_id)
            if reply is None or reply.topic_id != topic_id:
                return None
            if reply.author_kind != "agent" or reply.reply_to is None:
                return []
            rows = session.scalars(
                select(ModelCall)
                .where(
                    ModelCall.topic_id == topic_id,
                    ModelCall.message_id == reply.reply_to,
                    ModelCall.role == (reply.author_role or "")[:32],
                )
                .order_by(ModelCall.id)
            )
            return [call_view(row) for row in rows]

    def call(self, call_id: int) -> dict[str, Any] | None:
        """One call as the model got it, and what it answered."""
        with self.session_factory() as session:
            row = session.get(ModelCall, call_id)
            if row is None:
                return None
            request = dict(row.request_json or {})
            names = [*request.get("messages", []), request.get("schema"), request.get("tools")]
            wanted = {n for n in [*names, row.response_digest] if n}
            bodies = {
                p.digest: p.body
                for p in session.scalars(
                    select(ModelPayload).where(ModelPayload.digest.in_(wanted))
                )
            }
            return {
                **call_view(row),
                "messages": [bodies.get(name) for name in request.get("messages", [])],
                "max_tokens": request.get("max_tokens"),
                "schema": bodies.get(request.get("schema") or ""),
                "tools": bodies.get(request.get("tools") or ""),
                "response": bodies.get(row.response_digest or ""),
            }

    def recorded(self, run_id: UUID) -> dict[str, list[Completion]]:
        """A run's answers by the request that got them, in the order they came."""
        answers: dict[str, list[Completion]] = defaultdict(list)
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == run_id, ModelCall.status == OK)
                    .order_by(ModelCall.id)
                )
            )
            names = {row.response_digest for row in rows if row.response_digest}
            bodies = {
                p.digest: p.body
                for p in session.scalars(select(ModelPayload).where(ModelPayload.digest.in_(names)))
            }
        for row in rows:
            body = bodies.get(row.response_digest or "")
            if body is not None:
                answers[row.request_digest].append(Completion.model_validate(body))
        return dict(answers)


def call_view(row: ModelCall) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": str(row.run_id) if row.run_id else None,
        "origin": row.origin,
        "role": row.role,
        "model_alias": row.model_alias,
        "model": row.model,
        "status": row.status,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "latency_seconds": row.latency_seconds,
        "messages": len((row.request_json or {}).get("messages", [])),
        "at": row.created_at.isoformat() if row.created_at else None,
    }


def collect_unused(session: Session) -> int:
    """Remove bodies no kept call names any more (after a project is deleted)."""
    result = session.execute(
        text(
            "DELETE FROM model_payloads WHERE digest NOT IN ("
            " SELECT response_digest FROM model_calls WHERE response_digest IS NOT NULL"
            " UNION SELECT jsonb_array_elements_text(request_json->'messages') FROM model_calls"
            " UNION SELECT request_json->>'schema' FROM model_calls"
            "  WHERE request_json->>'schema' IS NOT NULL"
            " UNION SELECT request_json->>'tools' FROM model_calls"
            "  WHERE request_json->>'tools' IS NOT NULL)"
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


class KeptCompleter:
    """Wraps a role's model so each call it answers, or fails, is kept."""

    def __init__(
        self,
        inner: Any,
        calls: ModelCalls,
        run_id: UUID | None,
        role: str,
        *,
        topic_id: UUID | None = None,
        message_id: int | None = None,
        chat_ref: str | None = None,
    ) -> None:
        self.inner = inner
        self.calls = calls
        self.run_id = run_id
        self.role = role
        self.topic_id = topic_id
        self.message_id = message_id
        self.chat_ref = chat_ref

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: Any = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        request, bodies = request_of(model_alias, messages, max_tokens, schema, tools)
        started = monotonic()
        asked: dict[str, Any] = {}
        if max_tokens is not None:
            asked["max_tokens"] = max_tokens
        if schema is not None:
            asked["schema"] = schema
        if tools is not None:
            asked["tools"] = tools
        try:
            completion: Completion = await self.inner.complete(model_alias, messages, **asked)
        except GatewayError as failed:
            self._keep(request, bodies, None, failed.code, monotonic() - started)
            raise
        self._keep(request, bodies, completion, OK, monotonic() - started)
        return completion

    def _keep(
        self,
        request: dict[str, Any],
        bodies: dict[str, Any],
        completion: Completion | None,
        status: str,
        latency: float,
    ) -> None:
        try:
            self.calls.keep(
                self.run_id,
                self.role,
                request,
                bodies,
                completion,
                status,
                latency,
                topic_id=self.topic_id,
                message_id=self.message_id,
                chat_ref=self.chat_ref,
            )
        except Exception as error:  # the call itself must not fail for this
            logger.warning(
                json.dumps(
                    {
                        "event": "model_call_not_kept",
                        "run_id": str(self.run_id),
                        "error": type(error).__name__,
                    }
                )
            )


class ReplayCompleter:
    """Answers from a run's kept calls, without a model; refuses what was not asked."""

    def __init__(self, answers: dict[str, list[Completion]]) -> None:
        self.answers = {name: list(found) for name, found in answers.items()}

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: Any = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        request, _ = request_of(model_alias, messages, max_tokens, schema, tools)
        waiting = self.answers.get(digest(request))
        if not waiting:
            raise GatewayError(NOT_RECORDED)
        return waiting.pop(0)
