"""A sample office: what finished work looks like, before any key is entered.

A new owner faces a choice they cannot yet make — which provider, which model,
what it will cost — before they have seen a single thing the office produces.
Sample mode removes that: one project, one finished task, its artifacts and its
document, installed locally, costing nothing and calling nothing.

It is **visibly simulated and never presented as a live model test**. Every
piece carries the same marker: the project is named as a sample, the run is
labelled `sample`, each artifact is `created_by="sample"` with `sample: true` in
its metadata, and the work itself says in its first line that no model produced
it. The office would rather show less than let a person believe it has proved
something it has not.

Nothing here touches the gateway, records usage, or creates a model connection.
Removing it removes all of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.artifacts import ArtifactMetadata, ArtifactStore
from app.db.models import (
    Artifact,
    ModelCall,
    Project,
    RoomMessage,
    Run,
    RunControl,
    RunEvent,
    RunStateChange,
    SideEffect,
    Task,
    Topic,
    UsageEvent,
)
from app.project_data import remove_project_files

# The marker, in one place. Anything carrying it is sample work.
LABEL = "sample"
PROJECT_NAME = "Sample project (not real work)"
NOT_REAL = (
    "This is sample work, written when the office was installed. No model produced it "
    "and nothing was spent. It is here to show what a finished task looks like."
)
OBJECTIVE = "Which of the three payment providers suits a small EMI, and on what evidence?"


def payload() -> dict[str, Any]:
    """The synthesis a finished comparison produces, in the real shape.

    Written by hand, deliberately: a sample that went through the chain would be
    a live run, which is the one thing sample mode must not be.
    """
    return {
        "note": NOT_REAL,
        "recommendation": (
            "Provider B, on cost and on settlement time — but only its published terms were "
            "checked, so treat this as a shortlist and not a decision."
        ),
        "confidence": "medium",
        "question": OBJECTIVE,
        "comparison": {
            "columns": ["Provider", "Settlement", "Cost per transaction", "Evidence"],
            "rows": [
                ["Provider A", "T+2", "0.9%", "Published pricing page, read 2026-09-01"],
                ["Provider B", "T+1", "0.7%", "Published pricing page, read 2026-09-01"],
                ["Provider C", "not documented", "1.1%", "Pricing on request; not verified"],
            ],
        },
        "findings": [
            "Two of the three publish settlement times; the third gives them on request.",
            "Cost alone separates A and B by 0.2 percentage points on every transaction.",
        ],
        "unknowns": [
            "None of the three publish their onboarding times for an EMI.",
            "Provider C's pricing could not be verified from a public source.",
        ],
        "sources": [
            {"title": "Provider A pricing", "url": "https://example.invalid/a/pricing"},
            {"title": "Provider B pricing", "url": "https://example.invalid/b/pricing"},
        ],
    }


def installed(session: Session) -> Project | None:
    """The sample project, if this office has one."""
    return session.scalar(select(Project).where(Project.name == PROJECT_NAME))


def is_sample(row: Any) -> bool:
    """Whether a run or an artifact is sample work."""
    labels = getattr(row, "labels_json", None)
    if labels is not None:
        return LABEL in (labels or [])
    return bool((getattr(row, "metadata_json", None) or {}).get(LABEL))


def install(session: Session, store: ArtifactStore) -> dict[str, Any]:
    """Create the sample office, or return the one already here.

    Installing twice is not an error and does not produce two of anything: a
    new owner may well press the button again to see where it went.
    """
    existing = installed(session)
    if existing is not None:
        return {"project_id": str(existing.id), "created": False}

    project = Project(
        name=PROJECT_NAME,
        description=NOT_REAL,
        status="ACTIVE",
        deliverable_json={"shape": "comparison", "format": "xlsx"},
    )
    session.add(project)
    session.flush()

    task = Task(
        project_id=project.id,
        type="analysis",
        objective=OBJECTIVE,
        description=NOT_REAL,
        status="COMPLETED",
        requires_approval=False,
    )
    session.add(task)
    session.flush()

    finished = datetime.now(UTC)
    run = Run(
        project_id=project.id,
        root_task_id=task.id,
        workflow_name="analysis",
        status="COMPLETED",
        current_node="synthesis",
        started_at=finished - timedelta(minutes=6),
        completed_at=finished,
        labels_json=[LABEL],
        deliverable_json={"shape": "comparison", "format": "xlsx"},
        # No budget was consumed, and the run says so rather than leaving it
        # to be read as a task that happened to be free.
        budget_json={"max_cost_usd": 0, "spent_usd": 0, "note": NOT_REAL},
    )
    session.add(run)
    session.flush()

    artifact_id = _write(store, project.id, task.id, run.id, payload())
    session.add(
        Artifact(
            id=artifact_id,
            project_id=project.id,
            task_id=task.id,
            run_id=run.id,
            type="synthesis",
            path=str(
                store.path_of(
                    ArtifactMetadata(
                        artifact_id=artifact_id,
                        project_id=project.id,
                        type="synthesis",
                        created_by=LABEL,
                    )
                )
            ),
            created_by=LABEL,
            metadata_json={LABEL: True, "note": NOT_REAL},
        )
    )
    session.flush()
    return {"project_id": str(project.id), "run_id": str(run.id), "created": True}


def remove(session: Session, store: ArtifactStore) -> bool:
    """Take the sample office away, and everything that came with it."""
    project = installed(session)
    if project is None:
        return False
    remove_project_files(store.root, project.id)
    session.execute(delete(RoomMessage).where(RoomMessage.project_id == project.id))
    session.execute(delete(Topic).where(Topic.project_id == project.id))
    session.execute(delete(Artifact).where(Artifact.project_id == project.id))
    # A run's own history goes before it (its state's log, 5d; its writes, 5c).
    runs = select(Run.id).where(Run.project_id == project.id)
    for model in (RunControl, RunEvent, UsageEvent, RunStateChange, SideEffect, ModelCall):
        session.execute(delete(model).where(model.run_id.in_(runs)))
    session.execute(delete(Run).where(Run.project_id == project.id))
    session.execute(delete(Task).where(Task.project_id == project.id))
    session.execute(delete(Project).where(Project.id == project.id))
    session.flush()
    return True


def _write(
    store: ArtifactStore, project_id: UUID, task_id: UUID, run_id: UUID, body: dict[str, Any]
) -> UUID:
    import json

    metadata = ArtifactMetadata(
        artifact_id=uuid4(),
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        type="synthesis",
        created_by=LABEL,
    )
    store.write(metadata, json.dumps(body, indent=2, ensure_ascii=False))
    return metadata.artifact_id
