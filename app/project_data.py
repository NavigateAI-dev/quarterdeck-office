"""Owner export and deletion of one project.

Export writes the project's rows and artifact files to an archive. Deletion
requires the project's exact name, refuses while work is active, removes rows,
checkpoints and files, and reports external effects (merge requests) that
still exist outside the office, because deleting local history cannot
retract them.
"""

import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.orm import Session

from app.artifacts import ArtifactStore
from app.db.models import (
    Approval,
    Artifact,
    Decision,
    ModelCall,
    Project,
    ProjectMcpServer,
    ProjectSource,
    RoomMessage,
    Run,
    RunControl,
    RunEvent,
    RunStateChange,
    Schedule,
    SideEffect,
    Task,
    TaskTemplate,
    Topic,
    UsageEvent,
)
from app.model_calls import collect_unused

ACTIVE_STATUSES = ("PENDING", "RUNNING")
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


class ProjectDataError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _row(model: Any) -> dict[str, Any]:
    return {
        column.key: (str(value) if isinstance(value, UUID) else value)
        for column in inspect(model).mapper.column_attrs
        for value in [getattr(model, column.key)]
    }


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def export_project(session: Session, project: Project, store: ArtifactStore) -> bytes:
    runs = list(session.scalars(select(Run).where(Run.project_id == project.id)))
    run_ids = [run.id for run in runs]
    artifacts = list(session.scalars(select(Artifact).where(Artifact.project_id == project.id)))
    document = {
        "format": 1,
        "project": _row(project),
        "tasks": [
            _row(row) for row in session.scalars(select(Task).where(Task.project_id == project.id))
        ],
        "runs": [_row(row) for row in runs],
        "approvals": [
            _row(row)
            for row in session.scalars(select(Approval).where(Approval.run_id.in_(run_ids)))
        ],
        "decisions": [
            _row(row)
            for row in session.scalars(select(Decision).where(Decision.project_id == project.id))
        ],
        "artifacts": [_row(row) for row in artifacts],
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        members = [("project.json", _json(document).encode())]
        for artifact in artifacts:
            try:
                body = store.read(project.id, artifact.id, artifact.version)
            except (OSError, ValueError):
                continue  # Listed in project.json; a missing body is not invented.
            members.append(
                (f"artifacts/{artifact.type}-{artifact.id}-v{artifact.version}.md", body.encode())
            )
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _external_effects(
    project: Project, artifacts: list[Artifact], store: ArtifactStore
) -> list[str]:
    effects = []
    for artifact in artifacts:
        if artifact.type != "engineering_report":
            continue
        try:
            body = json.loads(store.read(project.id, artifact.id, artifact.version))
        except (OSError, ValueError):
            continue
        url = body.get("pull_request_url") if isinstance(body, dict) else None
        if url and url not in effects:
            effects.append(str(url))
    return effects


def _deleted(session: Session, statement: Any) -> int:
    return int(getattr(session.execute(statement), "rowcount", 0) or 0)


def delete_project(
    session: Session, project: Project, confirm: str, store: ArtifactStore
) -> dict[str, Any]:
    """Delete inside the caller's transaction; files are removed by the caller after commit."""
    if confirm != project.name:
        raise ProjectDataError("confirmation_mismatch")
    runs = list(session.scalars(select(Run).where(Run.project_id == project.id)))
    if any(run.status in ACTIVE_STATUSES for run in runs):
        raise ProjectDataError("project_has_active_runs")
    run_ids = [run.id for run in runs]
    artifacts = list(session.scalars(select(Artifact).where(Artifact.project_id == project.id)))
    effects = _external_effects(project, artifacts, store)
    # Writes to third parties that went through, or may have (5c): deleting the
    # office's record of them does not undo them.
    for subject in session.scalars(
        select(SideEffect.subject).where(
            SideEffect.run_id.in_(run_ids), SideEffect.status.in_(("done", "unknown"))
        )
    ):
        if subject not in effects:
            effects.append(subject)
    # Per-run history rows reference the runs, so they go first.
    for model in (RunControl, RunEvent, UsageEvent, RunStateChange, SideEffect, ModelCall):
        session.execute(delete(model).where(model.run_id.in_(run_ids)))
    counts = {
        "approvals": _deleted(session, delete(Approval).where(Approval.run_id.in_(run_ids))),
        "decisions": _deleted(session, delete(Decision).where(Decision.project_id == project.id)),
        "artifacts": _deleted(session, delete(Artifact).where(Artifact.project_id == project.id)),
        "runs": _deleted(session, delete(Run).where(Run.project_id == project.id)),
    }
    # Child tasks first: tasks may reference a parent task in the same project.
    session.execute(
        delete(Task).where(Task.project_id == project.id, Task.parent_task_id.is_not(None))
    )
    counts["tasks"] = _deleted(session, delete(Task).where(Task.project_id == project.id))
    existing = set(inspect(session.get_bind()).get_table_names())
    for table in CHECKPOINT_TABLES:
        if table in existing and run_ids:
            # Table names are constants; thread ids are bound parameters.
            session.execute(
                text(f"DELETE FROM {table} WHERE thread_id = ANY(:ids)"),  # noqa: S608
                {"ids": [str(run_id) for run_id in run_ids]},
            )
    session.execute(delete(ProjectSource).where(ProjectSource.project_id == project.id))
    # Grants and the room belong to the project and go with it; a tool server
    # itself is the office's and stays.
    session.execute(delete(ProjectMcpServer).where(ProjectMcpServer.project_id == project.id))
    # Its first topic (what its room was) goes with it. A topic that was only
    # placed in it is the owner's talk: it stays, belonging to no project.
    home = select(Topic.id).where(Topic.project_id == project.id, Topic.home)
    counts["room_messages"] = _deleted(
        session,
        delete(RoomMessage).where(
            RoomMessage.project_id == project.id,
            RoomMessage.topic_id.is_(None) | RoomMessage.topic_id.in_(home),
        ),
    )
    session.execute(
        update(RoomMessage).where(RoomMessage.project_id == project.id).values(project_id=None)
    )
    session.execute(delete(Topic).where(Topic.project_id == project.id, Topic.home))
    # What the runs' calls, and the room's (gone with its messages), sent and got
    # back, unless a call still kept names it too.
    collect_unused(session)
    session.execute(
        update(Topic)
        .where(Topic.project_id == project.id)
        .values(project_id=None, venture_id=None, scope_set_by=None)
    )
    # Templates outlive the project; their schedules stop with a reason.
    template_ids = list(
        session.scalars(select(TaskTemplate.id).where(TaskTemplate.project_id == project.id))
    )
    if template_ids:
        session.execute(
            update(TaskTemplate).where(TaskTemplate.id.in_(template_ids)).values(project_id=None)
        )
        session.execute(
            update(Schedule)
            .where(Schedule.template_id.in_(template_ids))
            .values(enabled=False, disabled_reason="project_deleted")
        )
    session.delete(project)
    return {"deleted": counts, "external_effects": effects}


def remove_project_files(store_root: Path, project_id: UUID) -> None:
    target = (store_root / str(project_id)).resolve()
    if store_root.resolve() in target.parents and target.exists():
        shutil.rmtree(target)
