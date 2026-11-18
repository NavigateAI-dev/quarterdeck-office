"""The owner's verdict on the lead's answers, and the numbers."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app import verdicts
from app.api.routers.deps import RouterDeps
from app.api.schemas import AnswerJudged


def add_verdict_routes(router: APIRouter, d: RouterDeps) -> None:
    @router.put("/topics/{topic_id}/messages/{message_id}/verdict")
    def judge_reply(topic_id: UUID, message_id: int, body: AnswerJudged) -> dict[str, Any]:
        """👍 or 👎 on a reply; the latest press wins."""
        with d.session_factory() as session, session.begin():
            try:
                row = verdicts.judge_reply(session, topic_id, message_id, body.verdict, body.note)
            except verdicts.VerdictRefused as refused:
                status = 404 if refused.code == "message_not_found" else 409
                raise HTTPException(status_code=status, detail=refused.code) from None
            return verdicts.view(row) or {}

    @router.post("/topics/{topic_id}/messages/{message_id}/redo", status_code=201)
    def redo_with_team(topic_id: UUID, message_id: int) -> dict[str, Any]:
        """The lead fell short: the full team takes up the same question, in this
        discussion, within its usual cap. Counts as a 👎 on the reply."""
        with d.session_factory() as session, session.begin():
            try:
                row = verdicts.judge_reply(session, topic_id, message_id, "down", None)
            except verdicts.VerdictRefused as refused:
                status = 404 if refused.code == "message_not_found" else 409
                raise HTTPException(status_code=status, detail=refused.code) from None
            if row.redo_run_id is not None:
                raise HTTPException(status_code=409, detail="already_redone")
            question = verdicts.question_of(session, message_id)
            note = row.note
        if not question:
            raise HTTPException(status_code=409, detail="no_question_to_redo")
        run = d.handoff.start_work(topic_id, message_id, question, full_team=True)
        if run is None:
            raise HTTPException(status_code=409, detail="not_started")
        with d.session_factory() as session, session.begin():
            row = verdicts.judge_reply(session, topic_id, message_id, "down", note)
            row.redo_run_id = run.id
            return verdicts.view(row) or {}

    @router.put("/runs/{run_id}/verdict")
    def judge_run(run_id: UUID, body: AnswerJudged) -> dict[str, Any]:
        """👍 or 👎 on a task's answer; the latest press wins."""
        with d.session_factory() as session, session.begin():
            try:
                row = verdicts.judge_run(session, run_id, body.verdict, body.note)
            except verdicts.VerdictRefused as refused:
                status = 404 if refused.code == "run_not_found" else 409
                raise HTTPException(status_code=status, detail=refused.code) from None
            return verdicts.view(row) or {}

    @router.get("/runs/{run_id}/verdict")
    def run_verdict(run_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            return {"verdict": verdicts.of_run(session, run_id)}

    @router.get("/quality/lead")
    def lead_quality(days: int = 30) -> dict[str, Any]:
        """Lead beside team on real use: per request, outcomes, verdicts, reworks."""
        with d.session_factory() as session:
            return verdicts.report(session, datetime.now(UTC), max(1, min(days, 90)))
