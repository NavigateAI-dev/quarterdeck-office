"""Messages for after the work, and turning one into a steer.

While work runs in a thread, the owner's Enter queues (said in the thread when
the work ends, `shared.deliver_queue`) and ⌘ Enter steers (taken at the next
step). Editing and withdrawing a waiting message are the controls routes.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agent_asks import AgentAsks, AskRefused
from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.api.schemas import QueuedMessage
from app.db.models import Run
from app.orchestrator.runner import QUEUED_MESSAGE, InstructionTaken
from app.rooms import STEERABLE


class AgentAnswer(BaseModel):
    option: str = Field(min_length=1, max_length=120)


def add_queue_routes(router: APIRouter, d: RouterDeps) -> None:
    asks = AgentAsks(d.session_factory, d.repository.add_event)

    @router.post("/runs/{run_id}/agent-asks/{ask_id}")
    def answer_agent(run_id: UUID, ask_id: str, body: AgentAnswer) -> dict[str, Any]:
        """The owner's answer to a coding agent's permission question."""
        try:
            asks.answer(run_id, ask_id, body.option)
        except AskRefused as refused:
            raise HTTPException(status_code=409, detail=refused.code) from None
        return {"answered": ask_id, "option": body.option}

    @router.post("/runs/{run_id}/queue", status_code=201)
    def queue_message(run_id: UUID, body: QueuedMessage) -> dict[str, Any]:
        """A message for after the work: said in its thread when it ends."""
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            if run.status not in STEERABLE:
                raise HTTPException(status_code=409, detail="run_not_working")
        return d.repository.add_control(run_id, QUEUED_MESSAGE, body.text, None)

    @router.post("/runs/{run_id}/controls/{control_id}/steer")
    def steer_queued_now(run_id: UUID, control_id: UUID) -> dict[str, Any]:
        """A queued message sent now instead: the next step takes it."""
        try:
            steered = d.repository.steer_queued(run_id, control_id)
        except InstructionTaken as taken:
            raise HTTPException(status_code=409, detail=f"already_{taken.status.lower()}") from None
        return found(steered, "Queued message")
