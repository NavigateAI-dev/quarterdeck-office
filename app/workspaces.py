"""Folders of the owner's, attached to a business space.

The owner approves one root (`~/dev`); it is mounted read-only into the
no-network reader. Inside it, folders are given to a space one by one, each
with an access:

- **read**: search, quote and summarise. On now.
- **work**: the coder on the owner's Mac changes files on an `office/<thread>`
  branch, and merging needs the owner's approval. Refused unless that helper
  is connected (Stage 36b; owner, 4 Nov) or the office has a writable mount.

Every grant, change and removal is kept in `workspace_grants`, so who allowed
what, and when, outlives the grant itself. A path is checked here, on the
server, before anything is stored; the reader checks again on every read
(symlinks out of the folder, hidden and sensitive files), because only it sees
the files.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Project, Topic, Venture, Workspace, WorkspaceGrant

READ = "read"
WORK = "work"
ACCESS = (READ, WORK)
# Folders that hold keys and sign-ins, refused wherever they sit (as the picker).
CREDENTIAL_NAMES = frozenset({".ssh", ".gnupg", ".aws", ".config", "Keychains", ".docker", ".kube"})
PATH_CHARS = 500


class WorkspaceRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def refusal(path: str) -> str | None:
    """Why this folder may not be given to a space, or None when it may."""
    if len(path) > PATH_CHARS:
        return "path_too_long"
    if path.startswith("/") or "\\" in path:
        return "absolute_path"
    parts = PurePosixPath(path.strip()).parts
    if not parts or parts == (".",):
        return "whole_root"
    if ".." in parts:
        return "outside_root"
    if any(part in CREDENTIAL_NAMES for part in parts):
        return "credential_folder"
    if any(part.startswith(".") for part in parts):
        return "hidden_folder"
    return None


def space_of(session: Session, project_id: UUID | None, topic_id: UUID | None) -> UUID | None:
    """The business space work belongs to: its thread's, else its project's (as the sidebar)."""
    topic = session.get(Topic, topic_id) if topic_id is not None else None
    if topic is not None and topic.venture_id is not None:
        return topic.venture_id
    project_ref = topic.project_id if topic is not None and topic.project_id else project_id
    project = session.get(Project, project_ref) if project_ref is not None else None
    return project.venture_id if project is not None else None


def view(row: Workspace) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "space_id": str(row.venture_id),
        "name": row.name,
        "path": row.path,
        "access": row.access,
        "default_branch": row.default_branch,
        "granted_by": row.granted_by,
        "granted_at": row.granted_at.isoformat() if row.granted_at else None,
    }


class Workspaces:
    """The spaces' folders, and the history of who allowed what."""

    def __init__(
        self, session_factory: sessionmaker[Session], writable: bool | Callable[[], bool] = False
    ) -> None:
        self.session_factory = session_factory
        # Whether work access can be done now: asked at each grant (36b).
        self.writable = writable

    def _check_access(self, access: str) -> None:
        if access not in ACCESS:
            raise WorkspaceRefused("access_unknown")
        possible = self.writable() if callable(self.writable) else self.writable
        if access == WORK and not possible:
            raise WorkspaceRefused("work_access_off")

    def attach(
        self, space_id: UUID, path: str, access: str, owner: str, name: str | None = None
    ) -> dict[str, Any]:
        cleaned = str(PurePosixPath(path.strip())) if path.strip() else ""
        refused = refusal(cleaned)
        if refused:
            raise WorkspaceRefused(refused)
        self._check_access(access)
        with self.session_factory() as session, session.begin():
            if session.get(Venture, space_id) is None:
                raise WorkspaceRefused("space_not_found")
            taken = session.scalar(
                select(Workspace.id).where(
                    Workspace.venture_id == space_id, Workspace.path == cleaned
                )
            )
            if taken is not None:
                raise WorkspaceRefused("already_attached")
            row = Workspace(
                venture_id=space_id,
                name=(name or PurePosixPath(cleaned).name)[:120],
                path=cleaned,
                access=access,
                granted_by=owner[:128],
                granted_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            self._record(session, row, "granted", owner)
            return view(row)

    def change(self, workspace_id: UUID, access: str, owner: str) -> dict[str, Any]:
        self._check_access(access)
        with self.session_factory() as session, session.begin():
            row = session.get(Workspace, workspace_id)
            if row is None:
                raise WorkspaceRefused("workspace_not_found")
            if row.access != access:
                row.access = access
                self._record(session, row, "access_changed", owner)
            return view(row)

    def detach(self, workspace_id: UUID, owner: str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(Workspace, workspace_id)
            if row is None:
                raise WorkspaceRefused("workspace_not_found")
            self._record(session, row, "revoked", owner)
            session.delete(row)

    def of_space(self, space_id: UUID) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(Workspace).where(Workspace.venture_id == space_id).order_by(Workspace.name)
            )
            return [view(row) for row in rows]

    def granted(self, space_id: UUID) -> dict[str, str]:
        """Each of the space's folders by name, and its path inside the root, as
        the grants stand now: read again at every use, so a folder taken away is
        refused at once (6b)."""
        with self.session_factory() as session:
            rows = session.execute(
                select(Workspace.name, Workspace.path).where(Workspace.venture_id == space_id)
            ).all()
        return {name: path for name, path in rows}

    def all(self) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(select(Workspace).order_by(Workspace.name))
            return [view(row) for row in rows]

    def history(self, space_id: UUID) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(WorkspaceGrant)
                .where(WorkspaceGrant.venture_id == space_id)
                .order_by(WorkspaceGrant.id.desc())
            )
            return [
                {
                    "workspace_id": str(row.workspace_id),
                    "path": row.path,
                    "action": row.action,
                    "access": row.access,
                    "by": row.by,
                    "detail": row.detail,
                    "at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    def folder(self, workspace_id: UUID) -> Workspace | None:
        with self.session_factory() as session:
            row = session.get(Workspace, workspace_id)
            if row is not None:
                session.expunge(row)
            return row

    def merged(self, workspace_id: UUID, branch: str, owner: str) -> None:
        """Keep the owner's merge in the folder's history."""
        with self.session_factory() as session, session.begin():
            row = session.get(Workspace, workspace_id)
            if row is not None:
                self._record(session, row, "merged", owner, detail=branch)

    @staticmethod
    def _record(
        session: Session, row: Workspace, action: str, owner: str, detail: str | None = None
    ) -> None:
        session.add(
            WorkspaceGrant(
                workspace_id=row.id,
                venture_id=row.venture_id,
                path=row.path,
                action=action,
                access=row.access,
                by=owner[:128],
                detail=detail[:255] if detail else None,
            )
        )
