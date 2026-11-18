"""A run's model calls, and those behind a discussion reply, as the models got them
(`app/model_calls.py`)."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.db.models import Run
from app.model_calls import ModelCalls


def add_model_call_routes(router: APIRouter, d: RouterDeps) -> None:
    calls = ModelCalls(d.session_factory)

    @router.get("/runs/{run_id}/model-calls")
    def model_calls(run_id: UUID) -> list[dict[str, Any]]:
        """Each call: role, model, tokens, time and outcome; not the bodies."""
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        return calls.of_run(run_id)

    @router.get("/topics/{topic_id}/messages/{message_id}/model-calls")
    def reply_model_calls(topic_id: UUID, message_id: int) -> list[dict[str, Any]]:
        """The calls behind an agent's reply; none for any other message."""
        listed = calls.of_reply(topic_id, message_id)
        if listed is None:
            raise HTTPException(status_code=404, detail="message_not_found")
        return listed

    @router.get("/model-calls/{call_id}")
    def model_call(call_id: int) -> dict[str, Any]:
        """One call in full: the messages, schema and tools sent, and the answer."""
        call = calls.call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="model_call_not_found")
        return call
