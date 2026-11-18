"""Ideas: kept on the floor, planned for a day, taken up on a press.

No `from __future__ import annotations` here: FastAPI must read the local
`Owner` annotation as a type, not a string.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import idea_sort, ideas, rooms, topics
from app.api.auth import OwnerIdentity
from app.api.schemas import (
    IdeaBlock,
    IdeaFinish,
    IdeaKeep,
    IdeaKind,
    IdeaPlan,
    IdeaProject,
    IdeaSortStart,
    IdeaText,
)
from app.db.models import CalendarChange, Idea, IdeaSort, Project, RoomMessage, Topic
from app.office_settings import MonthlyLimitReached, check_monthly_limit, paused


def add_idea_routes(
    router: APIRouter,
    session_factory: sessionmaker[Session],
    handoff: Any,
    require_owner: Callable[..., OwnerIdentity],
    complete: Callable[..., Any] | None = None,
    settings: Any = None,
) -> None:
    """`handoff` starts work in a discussion (`start_work`); only a press on
    Start reaches it. `complete(agent, messages)` is the one call a Sort makes,
    on the lead's model; only a confirmed press reaches it."""
    Owner = Annotated[OwnerIdentity, Depends(require_owner)]

    def idea_of(session: Session, idea_id: UUID) -> Idea:
        idea = session.get(Idea, idea_id)
        if idea is None:
            raise HTTPException(status_code=404, detail="Idea not found")
        return idea

    def shown(session: Session, idea: Idea) -> dict[str, Any]:
        topic = session.get(Topic, idea.topic_id) if idea.topic_id else None
        project = session.get(Project, idea.project_id) if idea.project_id else None
        return ideas.view(idea, ideas.owner_today(session, datetime.now(UTC)), topic, project)

    @router.get("/ideas")
    def idea_board() -> dict[str, Any]:
        """Today (overdue on top), new, later and someday."""
        with session_factory() as session:
            return ideas.board(session, datetime.now(UTC))

    @router.get("/ideas/sort")
    def idea_sort_view() -> dict[str, Any]:
        """What a sort would read and may cost, and the latest sort's suggestions."""
        cap = int(getattr(settings, "chat_max_output_tokens", 800) or 800)
        with session_factory() as session:
            return {
                **idea_sort.estimate(session, datetime.now(UTC), cap),
                "latest": idea_sort.view(session, idea_sort.latest(session)),
            }

    @router.post("/ideas/sort", status_code=201)
    async def idea_sort_start(body: IdeaSortStart) -> dict[str, Any]:
        """One model call on the lead's model, paid, so only on a confirmed press."""
        if not body.confirm:
            raise HTTPException(status_code=422, detail="confirm_required")
        if complete is None:
            raise HTTPException(status_code=409, detail="no_model")
        with session_factory() as session:
            if settings is not None and paused(session, settings, "chat"):
                raise HTTPException(status_code=409, detail="chat_paused")
            try:
                if settings is not None:
                    check_monthly_limit(session, settings)
            except MonthlyLimitReached:
                raise HTTPException(status_code=409, detail="monthly_limit_reached") from None
            from app.topic_briefs import lead

            agent = lead(session)
        if agent is None:
            raise HTTPException(status_code=409, detail="no_lead")
        try:
            row = await idea_sort.run(session_factory, datetime.now(UTC), agent, complete)
        except idea_sort.SortRejected as rejected:
            raise HTTPException(status_code=409, detail=rejected.code) from None
        with session_factory() as session:
            return idea_sort.view(session, session.get(IdeaSort, row.id)) or {}

    @router.post("/ideas/sort/{sort_id}/{n}/{answer}")
    def idea_sort_answer(sort_id: UUID, n: int, answer: str) -> dict[str, Any]:
        """Accept (applied now) or ignore one suggestion."""
        if answer not in ("accept", "ignore"):
            raise HTTPException(status_code=404, detail="Not found")
        with session_factory() as session, session.begin():
            row = session.get(IdeaSort, sort_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Sort not found")
            try:
                idea_sort.answer(session, row, n, answer == "accept", datetime.now(UTC))
            except idea_sort.SortRejected as rejected:
                raise HTTPException(status_code=409, detail=rejected.code) from None
            session.flush()
            return idea_sort.view(session, row) or {}

    @router.put("/ideas/{idea_id}/text")
    def idea_text(idea_id: UUID, body: IdeaText) -> dict[str, Any]:
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            if idea.status not in ideas.OPEN:
                raise HTTPException(status_code=409, detail="already_done")
            idea.text = body.text.strip()
            if not idea.text:
                raise HTTPException(status_code=422, detail="empty")
            session.flush()
            return shown(session, idea)

    @router.post("/ideas", status_code=201)
    def keep_idea(body: IdeaKeep) -> dict[str, Any]:
        """Keep a thought from the floor. Free: nothing reads it but the owner."""
        with session_factory() as session, session.begin():
            try:
                idea = ideas.keep(session, body.text, body.source, kind=body.kind)
            except ideas.IdeaRejected as rejected:
                raise HTTPException(status_code=422, detail=rejected.code) from None
            return shown(session, idea)

    @router.put("/ideas/{idea_id}/kind")
    def idea_kind(idea_id: UUID, body: IdeaKind) -> dict[str, Any]:
        """Friction, curiosity or opportunity, or back to unsorted."""
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            try:
                ideas.set_kind(idea, body.kind)
            except ideas.IdeaRejected as rejected:
                raise HTTPException(status_code=409, detail=rejected.code) from None
            session.flush()
            return shown(session, idea)

    @router.put("/ideas/{idea_id}/plan")
    def plan_idea(idea_id: UUID, body: IdeaPlan) -> dict[str, Any]:
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            try:
                ideas.plan(session, idea, body.day, body.someday, datetime.now(UTC))
            except ideas.IdeaRejected as rejected:
                raise HTTPException(status_code=409, detail=rejected.code) from None
            session.flush()
            return shown(session, idea)

    @router.put("/ideas/{idea_id}/project")
    def idea_project(idea_id: UUID, body: IdeaProject) -> dict[str, Any]:
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            project = None
            if body.project_id is not None:
                project = session.get(Project, body.project_id)
                if project is None:
                    raise HTTPException(status_code=404, detail="Project not found")
            try:
                ideas.set_project(idea, project)
            except ideas.IdeaRejected as rejected:
                raise HTTPException(status_code=409, detail=rejected.code) from None
            session.flush()
            return shown(session, idea)

    @router.put("/ideas/{idea_id}/block")
    def idea_block(idea_id: UUID, body: IdeaBlock) -> dict[str, Any]:
        """The event the owner just added from this idea; read from the change
        itself, never from what the browser says the time was."""
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            try:
                ideas.block(session, idea, session.get(CalendarChange, body.change_id))
            except ideas.IdeaRejected as rejected:
                raise HTTPException(status_code=409, detail=rejected.code) from None
            session.flush()
            return shown(session, idea)

    @router.post("/ideas/{idea_id}/finish")
    def finish_idea(idea_id: UUID, body: IdeaFinish) -> dict[str, Any]:
        """Done (the owner handled it) or dropped; it leaves the board."""
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            try:
                ideas.finish(idea, body.status)
            except ideas.IdeaRejected as rejected:
                raise HTTPException(status_code=409, detail=rejected.code) from None
            return {"id": str(idea_id), "status": idea.status}

    @router.post("/ideas/{idea_id}/drop")
    def drop_idea(idea_id: UUID) -> dict[str, Any]:
        """Kept for the desk's earlier Drop button."""
        return finish_idea(idea_id, IdeaFinish(status="dropped"))

    @router.post("/ideas/{idea_id}/start", status_code=201)
    def start_idea(idea_id: UUID, owner: Owner) -> dict[str, Any]:
        """Take an idea up as work, on the owner's press. One caught on the
        floor first gets a discussion of its own, opened with its words, so the
        work and its result have somewhere to come back to."""
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            if idea.status not in ideas.OPEN:
                raise HTTPException(status_code=409, detail="already_done")
            topic = session.get(Topic, idea.topic_id) if idea.topic_id else None
            if topic is None:
                project = session.get(Project, idea.project_id) if idea.project_id else None
                topic = topics.start(session, idea.text, project)
                said = rooms.post(
                    session, topic.project_id, idea.text, owner.name, topic_id=topic.id
                )
                # A room message's id is shown as "m:<id>".
                message_id = int(str(said["id"]).removeprefix("m:"))
                idea.topic_id, idea.source_message_id = topic.id, message_id
            topic_id, text = topic.id, idea.text
            origin = idea.source_message_id or session.scalar(
                select(RoomMessage.id)
                .where(RoomMessage.topic_id == topic_id)
                .order_by(RoomMessage.id.desc())
                .limit(1)
            )
        run = handoff.start_work(topic_id, int(origin or 0), text)
        if run is None:
            raise HTTPException(status_code=409, detail="not_started")
        with session_factory() as session, session.begin():
            idea = idea_of(session, idea_id)
            idea.status, idea.run_id = "taken", run.id
        return {"run_id": str(run.id), "topic_id": str(topic_id)}
