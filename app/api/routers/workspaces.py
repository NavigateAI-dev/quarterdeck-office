"""A space's folders: attach, change access, detach, and who did what.

The folders offered are those inside the approved root, as the read-only
reader sees them; a path is checked on the server before it is stored
(`app/workspaces.py`).
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routes import build_indexer, found
from app.coder import jobs as coder_jobs
from app.db.models import Run, RunEvent, Topic, Venture
from app.orchestrator.gate import current_branch
from app.policy import require
from app.sources.client import SourceError
from app.topics import runs_of
from app.workspace_git import BranchRefused, branch_diff, merge_branch, office_branches
from app.workspace_tools import READ_EVENT
from app.workspaces import WorkspaceRefused, Workspaces, space_of

MISSING = {"space_not_found", "workspace_not_found"}
# How many of a thread's reads its Files tab lists.
READS_SHOWN = 50


class Attach(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    access: Literal["read", "work"] = "read"
    name: str | None = Field(default=None, max_length=120)


class Access(BaseModel):
    access: Literal["read", "work"]


class Merge(BaseModel):
    branch: str = Field(min_length=1, max_length=255)


def add_workspace_routes(router: APIRouter, d: RouterDeps) -> None:
    def work_possible() -> bool:
        with d.session_factory() as session:
            return coder_jobs.work_possible(
                session, bool(d.settings.workspaces_writable), datetime.now(UTC)
            )

    store = Workspaces(d.session_factory, writable=work_possible)
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    def refused(error: WorkspaceRefused) -> HTTPException:
        return HTTPException(status_code=404 if error.code in MISSING else 409, detail=error.code)

    @router.get("/workspaces/folders")
    async def folders() -> dict[str, Any]:
        """The folders inside the approved root, as the reader sees them."""
        try:
            return {"folders": await build_indexer(d.settings).folders()}
        except SourceError as error:
            raise HTTPException(status_code=503, detail=error.code) from None

    @router.get("/workspaces")
    def every_workspace() -> dict[str, Any]:
        return {"workspaces": store.all(), "work_access": work_possible()}

    @router.get("/topics/{topic_id}/workspaces")
    def thread_workspaces(topic_id: UUID) -> dict[str, Any]:
        """A thread's space, its folders, and the files its work read (the dock's Files)."""
        with d.session_factory() as session:
            topic = found(session.get(Topic, topic_id), "Topic")
            space_id = space_of(session, topic.project_id, topic.id)
            space = session.get(Venture, space_id) if space_id is not None else None
            rows = session.execute(
                select(RunEvent.payload_json, RunEvent.created_at, RunEvent.run_id)
                .join(Run, Run.id == RunEvent.run_id)
                .where(runs_of(topic), RunEvent.kind == READ_EVENT)
                .order_by(RunEvent.id.desc())
                .limit(READS_SHOWN * 4)
            ).all()
        read: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for payload, at, run_id in rows:
            key = (str(payload.get("folder") or ""), str(payload.get("path") or ""))
            if key in seen:
                continue
            seen.add(key)
            read.append(
                {"folder": key[0], "path": key[1], "at": at.isoformat(), "run_id": str(run_id)}
            )
        return {
            "space": {"id": str(space.id), "name": space.name} if space is not None else None,
            "folders": store.of_space(space.id) if space is not None else [],
            "read": read[:READS_SHOWN],
            "work_access": work_possible(),
        }

    @router.get("/ventures/{space_id}/workspaces")
    def space_workspaces(space_id: UUID) -> dict[str, Any]:
        return {
            "workspaces": store.of_space(space_id),
            "history": store.history(space_id),
            "work_access": work_possible(),
        }

    @router.post("/ventures/{space_id}/workspaces", status_code=201)
    def attach(space_id: UUID, body: Attach, who: Owner) -> dict[str, Any]:
        try:
            return store.attach(space_id, body.path, body.access, who.name, body.name)
        except WorkspaceRefused as error:
            raise refused(error) from None

    @router.patch("/workspaces/{workspace_id}")
    def change(workspace_id: UUID, body: Access, who: Owner) -> dict[str, Any]:
        try:
            return store.change(workspace_id, body.access, who.name)
        except WorkspaceRefused as error:
            raise refused(error) from None

    def folder_path(workspace_id: UUID) -> str:
        row = store.folder(workspace_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workspace_not_found")
        return str(row.path)

    @router.get("/workspaces/{workspace_id}/files")
    async def folder_files(workspace_id: UUID) -> dict[str, Any]:
        """A folder's readable files, as it is now (the dock's Files, 7b)."""
        path = folder_path(workspace_id)
        try:
            found = await build_indexer(d.settings).files(path)
        except SourceError as error:
            raise HTTPException(status_code=503, detail=error.code) from None
        if found.get("error"):
            raise HTTPException(status_code=409, detail=found["error"])
        return found

    @router.get("/workspaces/{workspace_id}/read")
    async def folder_read(
        workspace_id: UUID, path: str = Query(min_length=1, max_length=500), offset: int = 0
    ) -> dict[str, Any]:
        """One file of a folder, read-only, from an offset; the reader's refusals stand."""
        root = folder_path(workspace_id)
        try:
            found = await build_indexer(d.settings).read(root, path, max(0, offset))
        except SourceError as error:
            raise HTTPException(status_code=503, detail=error.code) from None
        if found.get("error"):
            raise HTTPException(status_code=409, detail=found["error"])
        return found

    def work_repo(workspace_id: UUID) -> tuple[Path, str | None]:
        """The folder as the writable mount holds it; refused while work is off."""
        row = store.folder(workspace_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workspace_not_found")
        root = d.settings.workspaces_write_root
        if not d.settings.workspaces_writable or root is None or row.access != "work":
            raise HTTPException(status_code=409, detail="work_access_off")
        return root / row.path, row.default_branch

    @router.get("/workspaces/{workspace_id}/branches")
    async def branches(workspace_id: UUID) -> dict[str, Any]:
        """The office's branches in this folder's repository (6d)."""
        repo, _ = work_repo(workspace_id)
        try:
            return {"branches": await office_branches(repo)}
        except BranchRefused as error:
            raise HTTPException(status_code=409, detail=error.code) from None

    @router.get("/workspaces/{workspace_id}/branches/diff")
    async def branch_changes(workspace_id: UUID, branch: str) -> dict[str, Any]:
        """What an office/ branch changes, against the folder's default branch."""
        repo, default = work_repo(workspace_id)
        into = default or current_branch(repo) or "main"
        try:
            return await branch_diff(repo, branch, into)
        except BranchRefused as error:
            raise HTTPException(status_code=409, detail=error.code) from None

    @router.post("/workspaces/{workspace_id}/merge")
    async def merge(workspace_id: UUID, body: Merge, who: Owner) -> dict[str, Any]:
        """The owner merges an office/ branch into the default branch: fast-forward only,
        never into a checkout with changes. The owner's own act, kept in the history."""
        repo, default = work_repo(workspace_id)
        # This request is the owner's approval for this one merge.
        require("merge_workspace_branch", approved=True)
        into = default or current_branch(repo) or "main"
        try:
            commit = await merge_branch(repo, body.branch, into)
        except BranchRefused as error:
            raise HTTPException(status_code=409, detail=error.code) from None
        store.merged(workspace_id, body.branch, who.name)
        return {"merged": body.branch, "into": into, "commit": commit}

    @router.delete("/workspaces/{workspace_id}", status_code=204)
    def detach(workspace_id: UUID, who: Owner) -> None:
        try:
            store.detach(workspace_id, who.name)
        except WorkspaceRefused as error:
            raise refused(error) from None
