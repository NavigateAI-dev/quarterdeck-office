"""Routes for what work produces: approvals, artifacts, deliverables, usage."""

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from app import owner_documents
from app.api.auth import OwnerIdentity
from app.api.report_pages import page_path
from app.api.routers.deps import RouterDeps
from app.api.routes import DOWNLOAD_TYPES, attachment, found, slug
from app.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalView,
    ArtifactContent,
    ArtifactView,
    BenchmarkStart,
    DocumentEdit,
    PendingApproval,
    RevisionRequest,
)
from app.approvals import ApprovalScope, approval_decision, build_brief
from app.db.models import Artifact, Project, Run, Task
from app.deliverable import DEFAULT, FORMATS, offered
from app.documents import body_html, document_for, to_html, to_markdown
from app.documents.charts import chart_svgs
from app.documents.edited import as_dict as edited_as_dict
from app.documents.office_files import to_docx, to_pptx, to_xlsx
from app.office_settings import MonthlyLimitReached, OfficePaused
from app.orchestrator.approval_store import ApprovalConflict, ApprovalStatus
from app.run_start import start_run
from app.usage_report import usage_report
from app.work_performance import BENCHMARK, BENCHMARK_LABEL, BENCHMARK_PROJECT, work_performance


def _artifact_document(d: RouterDeps, artifact_id: UUID) -> tuple[Any, str]:
    """One artifact as a document, with a filename taken from its task."""
    with d.session_factory() as session:
        artifact = found(session.get(Artifact, artifact_id), "Artifact")
        project_id, version, kind = artifact.project_id, artifact.version, artifact.type
        # The objective lives on the task; the run points at it.
        task = session.get(Task, artifact.task_id) if artifact.task_id else None
        objective = task.objective if task is not None else None
    try:
        body = d.repository.store.read(project_id, artifact_id, version)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="artifact_content_missing") from None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # An older artifact, or one written as prose: shown as it stands.
        payload = {"text": body}
    title = (objective or "").strip().splitlines()[0][:120] if objective else None
    fields = payload if isinstance(payload, dict) else {"text": body}
    document = document_for(kind, fields, title)
    return document, f"{slug(document.title) or kind}-v{version}"


def resolve(
    d: RouterDeps,
    approval_id: UUID,
    status: str,
    body: ApprovalDecisionRequest,
    owner: OwnerIdentity,
    revision_target: str | None = None,
    decision: dict[str, Any] | None = None,
) -> Any:
    try:
        return d.approvals.resolve(
            approval_id,
            status,
            resolved_by=owner.name,
            expected_digest=body.scope_digest,
            comment=body.comment,
            revision_target=revision_target,
            decision=decision,
        )
    except ApprovalConflict as conflict:
        status_code = 404 if conflict.reason == "unknown_approval" else 409
        raise HTTPException(status_code=status_code, detail=conflict.reason) from None


def add_outputs_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/performance/work")
    def work_performance_view(days: int = 30) -> dict[str, Any]:
        """Step times, page reading and failures, from the office's own records."""

        with d.session_factory() as session:
            return work_performance(session, max(1, min(days, 365)))

    @router.post("/performance/benchmark", status_code=202)
    def run_benchmark(body: BenchmarkStart) -> dict[str, Any]:
        """Start the benchmark's questions as tasks. Paid work: only on the
        owner's press, which says it is (`confirm`)."""

        with d.session_factory() as session, session.begin():
            project = session.scalar(select(Project).where(Project.name == BENCHMARK_PROJECT))
            if project is None:
                project = Project(name=BENCHMARK_PROJECT)
                session.add(project)
                session.flush()
            project_id = project.id
        started = []
        try:
            for question in BENCHMARK:
                run = start_run(
                    d.session_factory, d.repository, d.worker, d.settings, project_id, question
                )
                with d.session_factory() as session, session.begin():
                    row = session.get(Run, run.id)
                    if row is not None:
                        row.labels_json = [BENCHMARK_LABEL]
                started.append(str(run.id))
        except OfficePaused:
            raise HTTPException(status_code=409, detail="office_paused") from None
        except MonthlyLimitReached:
            raise HTTPException(status_code=409, detail="monthly_limit_reached") from None
        return {"started": started, "project_id": str(project_id)}

    @router.get("/usage/summary")
    def usage_summary(days: int = 30) -> dict[str, Any]:
        """Token use, cost and model performance over a window."""
        with d.session_factory() as session:
            return usage_report(session, max(1, min(days, 365)), d.worker.waiting_count)

    @router.get("/documents/{lineage}")
    def owner_document(lineage: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            shown = owner_documents.view(session, lineage)
        if shown is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return shown

    @router.put("/documents/{lineage}")
    def edit_owner_document(lineage: UUID, body: DocumentEdit) -> dict[str, Any]:
        """The owner's edit of the document, as its next version."""
        try:
            shown = owner_documents.save(
                d.session_factory, d.repository.store, lineage, body.base_version, body.document
            )
        except owner_documents.StaleDocument as stale:
            raise HTTPException(status_code=409, detail=f"newer_version:{stale.args[0]}") from None
        except ValueError as invalid:
            raise HTTPException(status_code=422, detail=str(invalid)) from None
        if shown is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return shown

    @router.get("/deliverables")
    def deliverable_options() -> dict[str, Any]:
        """The shapes the office can produce, and how each can be handed over."""
        return {
            "shapes": offered(),
            "formats": [{"id": key, "label": label} for key, label in FORMATS.items()],
            "default": DEFAULT,
        }

    @router.get("/artifacts/{artifact_id}/document")
    def artifact_document(artifact_id: UUID) -> dict[str, Any]:
        """The artifact laid out for reading, rather than its stored payload."""
        document, _ = _artifact_document(d, artifact_id)
        return {
            "title": document.title,
            "subtitle": document.subtitle,
            "html": body_html(document),
            # The same document as data, for the editor.
            "document": edited_as_dict(document),
            # Each table of numbers drawn, for the editor to show under it.
            "charts": chart_svgs(document),
        }

    @router.get("/artifacts/{artifact_id}/page")
    def artifact_page(artifact_id: UUID) -> dict[str, Any]:
        """The private link to this answer as a page of its own."""

        with d.session_factory() as session:
            found(session.get(Artifact, artifact_id), "Artifact")
        path = page_path(d.settings, artifact_id)
        if path is None:
            raise HTTPException(status_code=409, detail="no_owner_secret")
        return {"path": path}

    @router.get("/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: UUID, format: str = "md") -> Response:
        """The artifact as a file the owner can read, keep, or send on."""
        if format not in DOWNLOAD_TYPES:
            raise HTTPException(status_code=422, detail="unsupported_format")
        document, name = _artifact_document(d, artifact_id)
        media_type, extension = DOWNLOAD_TYPES[format]
        writer = {
            "md": lambda doc: to_markdown(doc).encode(),
            "html": lambda doc: to_html(doc).encode(),
            "docx": to_docx,
            "xlsx": to_xlsx,
            "pptx": to_pptx,
        }[format]
        return Response(
            content=writer(document),
            media_type=media_type,
            headers={"Content-Disposition": attachment(name, extension)},
        )

    @router.get("/artifacts/{artifact_id}", response_model=ArtifactContent)
    def read_artifact(artifact_id: UUID) -> dict[str, Any]:
        """Return one artifact version with its stored body.

        The body is read through the project-scoped store, so an artifact can
        only be reached by the project that owns it.
        """
        with d.session_factory() as session:
            artifact = found(session.get(Artifact, artifact_id), "Artifact")
            detail = ArtifactView.model_validate(artifact).model_dump()
            detail["task_id"] = artifact.task_id
        try:
            detail["content"] = d.repository.store.read(
                artifact.project_id, artifact.id, artifact.version
            )
        except (OSError, ValueError):
            detail["content"] = ""
        return detail

    @router.get("/approvals/pending", response_model=list[PendingApproval])
    def pending_approvals(owner: Owner) -> list[dict[str, Any]]:
        items = []
        for approval in d.approvals.pending():
            scope = ApprovalScope.model_validate(approval.scope_json)
            with d.session_factory() as session:
                run = session.get(Run, approval.run_id)
                state = (run.state_json or {}) if run else {}
            brief = build_brief(scope, state.get("research"), state.get("critique"))
            items.append(
                {
                    "approval": ApprovalView.model_validate(approval).model_dump(),
                    "brief": brief.model_dump(),
                }
            )
        return items

    @router.post("/approvals/{approval_id}/approve", response_model=ApprovalView)
    def approve(
        approval_id: UUID,
        body: ApprovalDecisionRequest,
        owner: Owner,
    ) -> Any:
        """Explicitly approve one proposal.

        A decision record is created here and nowhere else: only an explicit
        human approval promotes a conclusion into a durable decision.
        """
        approval = d.approvals.get(approval_id)
        scope = ApprovalScope.model_validate(approval.scope_json) if approval else None
        decision = approval_decision(scope, body.comment) if scope is not None else None
        resolved = resolve(d, approval_id, ApprovalStatus.APPROVED, body, owner, decision=decision)
        d.worker.resume(resolved.run_id)
        return resolved

    @router.post("/approvals/{approval_id}/reject", response_model=ApprovalView)
    def reject(
        approval_id: UUID,
        body: ApprovalDecisionRequest,
        owner: Owner,
    ) -> Any:
        resolved = resolve(d, approval_id, ApprovalStatus.REJECTED, body, owner)
        d.worker.resume(resolved.run_id)
        return resolved

    @router.post("/approvals/{approval_id}/revise", response_model=ApprovalView)
    def revise(
        approval_id: UUID,
        body: RevisionRequest,
        owner: Owner,
    ) -> Any:
        """Send the work back for a new analysis cycle.

        A revision invalidates the approval and the artifacts downstream of the
        target; the next cycle must produce a new critique and a new approval.
        """
        resolved = resolve(
            d,
            approval_id,
            ApprovalStatus.REVISION_REQUESTED,
            body,
            owner,
            revision_target=body.revision_target,
        )
        d.worker.resume(resolved.run_id)
        return resolved
