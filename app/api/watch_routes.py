"""Watching pages and topics: started on the owner's press, then on its own."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from app import watches
from app.api.schemas import StrictRequest
from app.db.models import Watch


class WatchStart(StrictRequest):
    """A watch to start: what, how often, what to be told, where, the daily cap.
    `confirm` is the owner's press after reading what it may cost."""

    title: str = Field(default="", max_length=200)
    kind: str = Field(pattern="^(page|search)$")
    target: str = Field(min_length=1, max_length=2000)
    condition: str = Field(default="", max_length=1000)
    every_minutes: int = 240
    post_to_id: UUID | None = None
    ends_on: date | None = None
    daily_cap_usd: float = watches.CAP_DEFAULT
    confirm: bool = False


def add_watch_routes(
    router: APIRouter,
    session_factory: sessionmaker[Session],
    settings: Any,
    agent_complete: Callable[..., Any] | None,
) -> None:
    def found(session: Session, watch_id: UUID) -> Watch:
        watch = session.get(Watch, watch_id)
        if watch is None or watch.status == "deleted":
            raise HTTPException(status_code=404, detail="Watch not found")
        return watch

    @router.get("/watches")
    def list_watches() -> dict[str, Any]:
        with session_factory() as session:
            return {
                "watches": watches.listed(session),
                "every": list(watches.EVERY),
                "cap_default": watches.CAP_DEFAULT,
                "cap_max": watches.CAP_MAX,
            }

    @router.post("/watches", status_code=201)
    def start_watch(body: WatchStart) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(status_code=422, detail="confirm_required")
        with session_factory() as session, session.begin():
            try:
                watch = watches.create(
                    session,
                    title=body.title,
                    kind=body.kind,
                    target=body.target,
                    condition=body.condition,
                    every_minutes=body.every_minutes,
                    post_to_id=body.post_to_id,
                    ends_on=body.ends_on,
                    daily_cap_usd=body.daily_cap_usd,
                    now=datetime.now(UTC),
                )
            except watches.WatchRejected as rejected:
                raise HTTPException(status_code=422, detail=rejected.code) from None
            return watches.view(session, watch)

    @router.post("/watches/{watch_id}/{action}")
    async def act_on_watch(watch_id: UUID, action: str) -> dict[str, Any]:
        """pause, resume, check (now, on the owner's press), or delete."""
        if action not in ("pause", "resume", "check", "delete"):
            raise HTTPException(status_code=404, detail="Not found")
        now = datetime.now(UTC)
        with session_factory() as session, session.begin():
            watch = found(session, watch_id)
            if action == "pause":
                watch.status = "paused"
            elif action == "resume":
                watch.status, watch.stopped_reason, watch.failures = "active", None, 0
                watch.next_check_at = now
            elif action == "delete":
                # Kept, so what its checks cost still counts in the month.
                watch.status = "deleted"
                return {"id": str(watch_id), "status": "deleted"}
            elif watch.status != "active":
                raise HTTPException(status_code=409, detail="not_active")
        if action == "check":
            read, search, complete = watches.tools(settings, session_factory, agent_complete)
            await watches.check(
                session_factory,
                watch_id,
                now,
                read,
                search,
                complete,
                watches.month_left_for(settings),
            )
        with session_factory() as session:
            return watches.view(session, found(session, watch_id))
