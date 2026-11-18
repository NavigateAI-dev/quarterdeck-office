"""Routines: recurring work, from the owner's page."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routers.projects import checked_post_to
from app.api.routes import found
from app.api.schemas import FollowUpSave, RoutineSave
from app.db.models import Project, Schedule, Topic
from app.followups import service as followups
from app.routines import presets, service
from app.routines.run import fire, send_now


def _routine(session: Session, routine_id: UUID) -> Schedule:
    try:
        return service.found(session, routine_id)
    except service.RoutineRejected:
        raise HTTPException(status_code=404, detail="Routine not found") from None


def add_routines_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/routines")
    def list_routines() -> dict[str, Any]:
        with d.session_factory() as session:
            return {
                "routines": [service.view(session, row) for row in service.listed(session)],
                "presets": presets.listed(),
            }

    @router.post("/routines", status_code=201)
    def create_routine(body: RoutineSave, owner: Owner) -> dict[str, Any]:
        if not body.title or not body.prompt:
            raise HTTPException(status_code=422, detail="routine_needs_title_and_prompt")
        with d.session_factory() as session, session.begin():
            project = (
                found(session.get(Project, body.project_id), "Project") if body.project_id else None
            )
            if body.topic_id:
                found(session.get(Topic, body.topic_id), "Discussion")
            try:
                rule = service.rule_from(
                    body.model_dump(), body.timezone or service.owner_timezone(session)
                )
                row = service.create(
                    session,
                    title=body.title,
                    prompt=body.prompt,
                    quiet_when=body.quiet_when or "",
                    rule=rule,
                    now=datetime.now(UTC),
                    topic_id=body.topic_id,
                    project=project,
                    post_to=checked_post_to(d, session, body.post_to),
                    preset=body.preset,
                    enabled=True if body.enabled is None else body.enabled,
                )
            except service.RoutineRejected as wrong:
                raise HTTPException(status_code=422, detail=str(wrong)) from None
            return service.view(session, row)

    @router.put("/routines/{routine_id}")
    def change_routine(routine_id: UUID, body: RoutineSave, owner: Owner) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        with d.session_factory() as session, session.begin():
            row = _routine(session, routine_id)
            if "post_to" in changes:
                changes["post_to"] = checked_post_to(d, session, body.post_to)
            try:
                service.update(session, row, changes, datetime.now(UTC))
            except service.RoutineRejected as wrong:
                raise HTTPException(status_code=422, detail=str(wrong)) from None
            return service.view(session, row)

    @router.delete("/routines/{routine_id}", status_code=204)
    def delete_routine(routine_id: UUID, owner: Owner) -> None:
        with d.session_factory() as session, session.begin():
            session.delete(_routine(session, routine_id))

    @router.get("/routines/{routine_id}/history")
    def routine_history(routine_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            return {"history": service.history(session, _routine(session, routine_id))}

    @router.post("/routines/{routine_id}/send", status_code=202)
    def send_routine(routine_id: UUID, owner: Owner, background: BackgroundTasks) -> dict[str, Any]:
        """Make one now, as it would come due; its timing stays as it is."""
        with d.session_factory() as session:
            _routine(session, routine_id)
        try:
            message = send_now(d.session_factory, routine_id, datetime.now(UTC))
        except service.RoutineRejected as wrong:
            raise HTTPException(status_code=409, detail=str(wrong)) from None
        background.add_task(fire, d.session_factory, d.replies, routine_id, message)
        return {"message_id": message}

    @router.get("/follow-ups")
    def list_follow_ups() -> dict[str, Any]:
        now = datetime.now(UTC)
        with d.session_factory() as session, session.begin():
            followups.gather(session, now)
            return followups.listed(session, now)

    @router.put("/follow-ups/{follow_up_id}")
    def change_follow_up(follow_up_id: UUID, body: FollowUpSave, owner: Owner) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            try:
                row = followups.found(session, follow_up_id)
            except followups.FollowUpRejected:
                raise HTTPException(status_code=404, detail="To-do not found") from None
            try:
                followups.record(
                    session,
                    row,
                    datetime.now(UTC),
                    status=body.status,
                    note=body.note or "",
                    next_on=body.next_on,
                )
            except followups.FollowUpRejected as wrong:
                raise HTTPException(status_code=422, detail=str(wrong)) from None
            return followups.view(row)
