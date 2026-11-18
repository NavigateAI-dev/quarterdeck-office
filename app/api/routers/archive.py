"""AI history: the owner's exports, imported and searched. Owner only, reading included."""

import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routes import build_indexer
from app.api.schemas import (
    ArchiveConversationSave,
    ArchiveMemorySave,
    ArchiveProtectionSave,
    ArchiveReadStart,
)
from app.archive import exports, memories, protection, service
from app.db.models import ArchiveImport
from app.office_settings import MonthlyLimitReached, check_monthly_limit, paused
from app.sources.reader import ARCHIVE
from app.topic_briefs import lead

logger = logging.getLogger("quarterdeck.archive")


def add_archive_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/archive")
    def archive_overview(owner: Owner) -> dict[str, Any]:
        with d.session_factory() as session:
            return {
                **service.overview(session, datetime.now(UTC)),
                "review": service.to_review(session),
                "memories": memories.review(session),
            }

    @router.post("/archive/imports", status_code=202)
    async def import_export(
        request: Request, filename: str, owner: Owner, background: BackgroundTasks
    ) -> dict[str, str]:
        """The file arrives as the body and goes to disk unopened; the reader parses it."""
        folder = d.settings.sources_exchange / ARCHIVE
        folder.mkdir(parents=True, exist_ok=True)
        with d.session_factory() as session, session.begin():
            import_id, name = service.new_upload(session, filename)
        path = folder / f"{name}.upload"
        size = 0
        try:
            with path.open("wb") as stored:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > service.MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="export_too_large")
                    stored.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            with d.session_factory() as session, session.begin():
                row = session.get(ArchiveImport, import_id)
                if row is not None:
                    row.status, row.error = "failed", "upload_failed"
            raise
        background.add_task(
            service.run_import,
            d.session_factory,
            build_indexer(d.settings),
            d.settings.sources_exchange,
            import_id,
            name,
        )
        return {"id": str(import_id)}

    @router.post("/archive/imports/{import_id}/open")
    def open_import(import_id: UUID, owner: Owner) -> dict[str, int]:
        with d.session_factory() as session, session.begin():
            return {"opened": service.open_import(session, import_id)}

    @router.get("/archive/search")
    def search_archive(
        owner: Owner,
        q: str,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        project_id: UUID | None = None,
    ) -> dict[str, Any]:
        with d.session_factory() as session:
            return {
                "hits": service.search(
                    session,
                    q,
                    source=source,
                    since=since,
                    until=until,
                    project_id=project_id,
                    include_excluded=True,
                )
            }

    @router.get("/archive/conversations/{ref}")
    def read_conversation(ref: str, owner: Owner, around: int | None = None) -> dict[str, Any]:
        with d.session_factory() as session:
            try:
                return service.conversation(session, ref, around)
            except service.ArchiveRejected:
                raise HTTPException(status_code=404, detail="Conversation not found") from None

    @router.put("/archive/conversations/{conversation_id}")
    def change_conversation(
        conversation_id: UUID, body: ArchiveConversationSave, owner: Owner
    ) -> dict[str, bool]:
        with d.session_factory() as session, session.begin():
            try:
                service.set_excluded(session, conversation_id, body.excluded)
            except service.ArchiveRejected:
                raise HTTPException(status_code=404, detail="Conversation not found") from None
        return {"excluded": body.excluded}

    @router.delete("/archive/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: UUID, owner: Owner) -> None:
        with d.session_factory() as session, session.begin():
            try:
                service.forget(session, conversation_id)
            except service.ArchiveRejected:
                raise HTTPException(status_code=404, detail="Conversation not found") from None

    @router.post("/archive/conversations/{conversation_id}/memories", status_code=201)
    async def read_for_memories(
        conversation_id: UUID, body: ArchiveReadStart, owner: Owner
    ) -> dict[str, Any]:
        """One model call on the lead's model, over this one conversation."""
        if not body.confirm:
            raise HTTPException(status_code=422, detail="confirm_required")
        with d.session_factory() as session:
            if paused(session, d.settings, "chat"):
                raise HTTPException(status_code=409, detail="chat_paused")
            try:
                check_monthly_limit(session, d.settings)
            except MonthlyLimitReached:
                raise HTTPException(status_code=409, detail="monthly_limit_reached") from None
            agent = lead(session)
        if agent is None:
            raise HTTPException(status_code=409, detail="no_lead")
        try:
            return await memories.read(
                d.session_factory,
                conversation_id,
                agent,
                d.settings,
                lambda who, messages: d.replies.complete(who, messages),
            )
        except service.ArchiveRejected as rejected:
            raise HTTPException(status_code=409, detail=str(rejected)) from None
        except Exception:
            logger.exception(
                '{"event":"archive_reading_failed","conversation":"%s"}', conversation_id
            )
            raise HTTPException(status_code=502, detail="model_failed") from None

    @router.put("/archive/memories/{memory_id}")
    def save_memory(memory_id: UUID, body: ArchiveMemorySave, owner: Owner) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            try:
                return memories.decide(session, memory_id, body.status, body.content)
            except service.ArchiveRejected as rejected:
                code = 404 if "no such" in str(rejected) else 422
                raise HTTPException(status_code=code, detail=str(rejected)) from None

    @router.get("/archive/memories/export.md")
    def export_memories(owner: Owner, topic: str | None = None) -> Response:
        """What the owner kept, as a document to hand to another AI."""
        with d.session_factory() as session:
            text = memories.export_markdown(session, topic)
        return Response(
            content=text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="what-i-have-worked-out.md"'},
        )

    @router.get("/archive/protection")
    def read_protection(owner: Owner) -> dict[str, Any]:
        with d.session_factory() as session:
            return protection.view(session)

    @router.put("/archive/protection")
    def save_protection(body: ArchiveProtectionSave, owner: Owner) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            protection.save(session, body.model_dump())
        with d.session_factory() as session:
            return protection.view(session)

    @router.get("/archive/export.zip")
    def export_history(
        owner: Owner, background: BackgroundTasks, source: str | None = None, mask: bool = True
    ) -> FileResponse:
        """Everything kept, as files. Written to disk first: an archive may be large."""
        with d.session_factory() as session, session.begin():
            path = exports.write_zip(session, source, mask)
            protection.note(
                session,
                "You",
                "owner",
                "export",
                f"{source or 'all sources'}, {'masked' if mask else 'secrets as written'}",
            )
        background.add_task(path.unlink, missing_ok=True)
        return FileResponse(
            path,
            media_type="application/zip",
            filename="ai-history.zip",
            background=background,
        )
