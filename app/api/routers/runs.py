"""Routes for work: runs, tasks and intake."""

import json
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import attachments, builds, hosting, revisions, team_proposals
from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routers.shared import _home, save_template
from app.api.routes import DEFAULT_NOTE, INTAKE_CONFIDENCE, LIMIT_EDITABLE, found
from app.api.schemas import (
    AnswerEdit,
    AnswerRewrite,
    ArtifactView,
    BudgetRaise,
    BuildNote,
    BuildRestore,
    DirectionEdit,
    FollowUp,
    HumanReviewRequest,
    IntakeRequest,
    LabelsUpdate,
    PublishRequest,
    ReworkRequest,
    RunControlRequest,
    RunCreate,
    RunDetail,
    RunHelp,
    RunTitle,
    RunView,
    TaskCreate,
    TaskView,
    TeamRoomPost,
    TemplateFromRun,
)
from app.budget import room_to_finish
from app.credentials import open_secret_store
from app.db.models import Approval, Artifact, Project, Run, Task
from app.deliverable import clean as clean_deliverable
from app.entities import facts_from
from app.entity_store import known as known_entities
from app.entity_store import record as record_facts
from app.follow_up import thread_of
from app.judgments import JudgmentUnavailable, build_judge
from app.lineage import lineage
from app.live_build import live_view
from app.memory.evaluate import NothingLearned, evaluate, projection, trace_of
from app.memory.store import record
from app.office_settings import MonthlyLimitReached, OfficePaused
from app.orchestrator.graph import ANALYSIS_WORKFLOW
from app.orchestrator.review import ReviewRejected, ReviewStore
from app.orchestrator.runner import InstructionTaken, UnknownProject
from app.orchestrator.state import RunStatus
from app.project_context import run_context_summary
from app.project_routing import catalog
from app.project_settings import advisor_on, team_on
from app.run_actions import ActionRefused, record_review
from app.run_flow import run_flow
from app.run_start import AUTO_WORKFLOW, start_run
from app.secret_store import SecretStoreError
from app.status import office_status
from app.titles import title_for, title_of
from app.venture_routing import business_line


def with_title(d: RouterDeps, runs: list[Run], session: Session) -> list[Run]:
    """Attach each run's short title, read from its task in one query."""
    if not runs:
        return runs
    titles = {
        task_id: title_of(title, objective)
        for task_id, objective, title in session.execute(
            select(Task.id, Task.objective, Task.title).where(
                Task.id.in_({run.root_task_id for run in runs})
            )
        ).all()
    }
    for run in runs:
        run.title = titles.get(run.root_task_id, title_for(""))  # type: ignore[attr-defined]
    return runs


def latest_artifact_payload(d: RouterDeps, run_id: UUID, kind: str) -> dict[str, Any] | None:
    """The newest artifact of this type for a run, as its stored payload."""
    with d.session_factory() as session:
        row = session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run_id, Artifact.type == kind)
            .order_by(Artifact.version.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        project_id, version, artifact_id = row.project_id, row.version, row.id
    try:
        body = d.repository.store.read(project_id, artifact_id, version)
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _has_secret(d: RouterDeps, store: Any, name: str) -> bool:
    try:
        return bool(store.get(name))
    except SecretStoreError:
        return False


# ---- What a build made ----
def _build_folder(d: RouterDeps, run_id: UUID) -> tuple[Any, Path, dict[str, Any]]:

    with d.session_factory() as session:
        run = found(session.get(Run, run_id), "Run")
        state = dict(run.state_json or {})
    key = state.get("build_folder")
    if not key:
        raise HTTPException(status_code=404, detail="no_build")
    return builds, builds.folder_for(d.settings, str(key)), state


def reopen(d: RouterDeps, run_id: UUID, comment: str, target: str | None) -> str | None:
    return d.actions.reopen(run_id, comment, target)


async def judged_target(d: RouterDeps, note: str) -> tuple[str, str]:
    try:
        return await d.actions.judged_target(note)
    except ActionRefused as refused:
        raise HTTPException(status_code=refused.status, detail=refused.code) from None


def add_runs_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.post("/intake")
    async def intake(body: IntakeRequest) -> dict[str, Any]:
        """Route a free-text owner request to a handler (the chat entry point).

        Only proposes by default. Starting a task makes billable model calls, so
        it happens only with `execute: true`. Approving, rejecting or changing
        anything is never reachable from text.
        """
        judge = build_judge(d.settings)
        if judge is None:
            raise HTTPException(status_code=503, detail="intake_requires_typesafe")
        with d.session_factory() as session:
            known = catalog(session)
        projects = known.ids
        try:
            answers = await judge.route(body.text, known.described)
        except JudgmentUnavailable:
            raise HTTPException(status_code=503, detail="intake_unavailable") from None
        action, confidence = answers.get("action", ("none", 0.0))
        reply: dict[str, Any] = {"action": action, "confidence": round(confidence, 2)}
        reply["executed"] = False
        if confidence < INTAKE_CONFIDENCE:
            return reply | {"action": "clarify", "reason": "request_unclear"}
        if action == "office_status":
            with d.session_factory() as session:
                status = office_status(session, d.settings, d.worker.waiting_count)
            return reply | {"result": status}
        if action == "pending_approvals":
            pending = [
                {"approval_id": str(row.id), "run_id": str(row.run_id)}
                for row in d.approvals.pending()
            ]
            return reply | {"result": pending}
        if action != "start_task":
            return reply | {"action": "none", "reason": "not_available_from_text"}
        project_name, project_confidence = answers.get("project", ("none", 0.0))
        if project_name not in projects or project_confidence < INTAKE_CONFIDENCE:
            return reply | {"action": "clarify", "reason": "project_unclear"}
        proposal = {"project_id": str(projects[project_name]), "objective": body.text}
        reply["proposal"] = proposal
        if body.execute:
            run = create_run(RunCreate(project_id=projects[project_name], objective=body.text))
            reply |= {"executed": True, "run_id": str(run.id)}
        return reply

    @router.post("/tasks", response_model=TaskView, status_code=201)
    def create_task(body: TaskCreate) -> Task:
        with d.session_factory() as session, session.begin():
            found(session.get(Project, body.project_id), "Project")
            task = Task(
                project_id=body.project_id,
                type=body.type,
                objective=body.objective,
                acceptance_criteria=body.acceptance_criteria,
            )
            session.add(task)
            session.flush()
            session.refresh(task)
            return task

    @router.get("/tasks/{task_id}", response_model=TaskView)
    def get_task(task_id: UUID) -> Task:
        with d.session_factory() as session:
            return found(session.get(Task, task_id), "Task")

    @router.post("/runs", response_model=RunView, status_code=202)
    def create_run(body: RunCreate) -> Run:
        """Record durable intent and return immediately.

        The run row exists before this responds, so the work survives a restart
        even though execution happens after the response.
        """
        if body.continues_run_id is not None:
            with d.session_factory() as session:
                found(session.get(Run, body.continues_run_id), "Run")
        try:
            started = start_run(
                d.session_factory,
                d.repository,
                d.worker,
                d.settings,
                body.project_id,
                body.objective,
                body.budget.model_dump() if body.budget else None,
                serves_objective_id=body.serves_objective_id,
                deliverable=body.deliverable,
                title=body.title,
                continues_run_id=body.continues_run_id,
                use_browser=body.use_browser,
                attachments=body.attachments,
                workflow=ANALYSIS_WORKFLOW if body.full_team else AUTO_WORKFLOW,
            )
            started.title = title_of(body.title, body.objective)  # type: ignore[attr-defined]
            return started
        except UnknownProject:
            raise HTTPException(status_code=404, detail="Project not found") from None
        except attachments.AttachmentRejected as rejected:
            raise HTTPException(status_code=404, detail=rejected.code) from None
        except OfficePaused:
            raise HTTPException(status_code=409, detail="office_paused") from None
        except MonthlyLimitReached:
            raise HTTPException(status_code=409, detail="monthly_limit_reached") from None

    @router.post("/runs/{run_id}/follow-up", response_model=RunView, status_code=202)
    def follow_up(run_id: UUID, body: FollowUp) -> Run:
        """Ask the next question of a task that has already finished.

        A task in its own right — planned, budgeted and refusable like any
        other — that is told what the earlier one established.
        """
        with d.session_factory() as session:
            parent = found(session.get(Run, run_id), "Run")
            project_id = parent.project_id
            state = dict(parent.state_json or {})
            # A build's next step is its next version, not a report about it.
            built = (
                state.get("build_folder") or (state.get("plan") or {}).get("task_kind") == "build"
            )
            if parent.status not in ("COMPLETED", "FAILED", "CANCELLED"):
                # A task still working can be steered with a direction instead;
                # a follow-up would duplicate work already in flight.
                raise HTTPException(status_code=409, detail="still_working")
        try:
            started = start_run(
                d.session_factory,
                d.repository,
                d.worker,
                d.settings,
                project_id,
                body.objective,
                body.budget.model_dump() if body.budget else None,
                deliverable=body.deliverable
                or ({"shape": "software", "format": "onscreen"} if built else None),
                continues_run_id=run_id,
            )
            started.title = title_for(body.objective)  # type: ignore[attr-defined]
            return started
        except UnknownProject:
            raise HTTPException(status_code=404, detail="Project not found") from None
        except OfficePaused:
            raise HTTPException(status_code=409, detail="office_paused") from None
        except MonthlyLimitReached:
            raise HTTPException(status_code=409, detail="monthly_limit_reached") from None

    @router.get("/runs/{run_id}/thread")
    def run_thread(run_id: UUID) -> dict[str, Any]:
        """What this task continues, and what continued it."""
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
            return thread_of(session, run_id)

    @router.post("/runs/{run_id}/template", status_code=201)
    def template_from_run(run_id: UUID, body: TemplateFromRun) -> dict[str, Any]:
        """Save a run's objective, project and budget for reuse."""
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            task = session.get(Task, run.root_task_id)
            objective = task.objective if task else ""
            project_id, budget = run.project_id, dict(run.budget_json or {})
        return save_template(
            d,
            {
                "name": body.name,
                "project_id": project_id,
                "objective": objective,
                "budget_json": budget,
                "source_run_id": run_id,
            },
        )

    @router.get("/runs", response_model=list[RunView])
    def list_runs(project_id: UUID | None = None) -> list[Run]:
        with d.session_factory() as session:
            statement = select(Run).order_by(Run.started_at.desc().nullslast(), Run.id)
            if project_id is not None:
                statement = statement.where(Run.project_id == project_id)
            return with_title(d, list(session.scalars(statement)), session)

    @router.post("/runs/{run_id}/controls", status_code=201)
    def control_run(run_id: UUID, body: RunControlRequest) -> dict[str, Any]:
        """Instruct, pause, cancel or skip a run (`RunActions.control`)."""
        try:
            return d.actions.control(run_id, body.kind, body.text, body.target_node)
        except ActionRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.code) from None

    @router.put("/runs/{run_id}/labels", response_model=RunDetail)
    def set_labels(run_id: UUID, body: LabelsUpdate) -> dict[str, Any]:
        """Label a task, for finding and grouping work on the board."""
        with d.session_factory() as session, session.begin():
            run = found(session.get(Run, run_id), "Run")
            run.labels_json = body.labels
            session.flush()
        return get_run(run_id)

    @router.put("/runs/{run_id}/title", response_model=RunDetail)
    def rename_run(run_id: UUID, body: RunTitle) -> dict[str, Any]:
        """The owner renames a task; blank derives it from the ask again."""
        with d.session_factory() as session, session.begin():
            run = found(session.get(Run, run_id), "Run")
            task = found(session.get(Task, run.root_task_id), "Task")
            task.title = title_of(body.title, task.objective)
            session.flush()
        return get_run(run_id)

    @router.put("/runs/{run_id}/controls/{control_id}")
    def reword_direction(run_id: UUID, control_id: UUID, body: DirectionEdit) -> dict[str, Any]:
        """Change a note while it is still waiting for an agent."""
        try:
            changed = d.repository.change_instruction(run_id, control_id, body.text)
        except InstructionTaken as taken:
            raise HTTPException(status_code=409, detail=f"already_{taken.status.lower()}") from None
        return found(changed, "Direction")

    @router.delete("/runs/{run_id}/controls/{control_id}", status_code=204)
    def withdraw_direction(run_id: UUID, control_id: UUID) -> None:
        """Take a note back before any agent has it."""
        try:
            withdrawn = d.repository.withdraw_instruction(run_id, control_id)
        except InstructionTaken as taken:
            raise HTTPException(status_code=409, detail=f"already_{taken.status.lower()}") from None
        if not withdrawn:
            raise HTTPException(status_code=404, detail="Direction not found")

    @router.get("/runs/{run_id}/controls")
    def run_controls(run_id: UUID) -> list[dict[str, Any]]:
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        return d.repository.controls(run_id)

    @router.post("/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: UUID) -> dict[str, str]:
        with d.session_factory() as session:
            status = found(session.get(Run, run_id), "Run").status
        if status != RunStatus.PAUSED:
            raise HTTPException(status_code=409, detail="run_not_paused")
        # A run held at a ceiling would stop again on its first call, leaving
        # the owner looking at an unchanged task after being told it resumed.
        if d.repository.binding_ceiling(run_id, d.settings.llm_max_output_tokens):
            raise HTTPException(status_code=409, detail="raise_the_limit_first")
        if not d.worker.resume_paused(run_id):
            raise HTTPException(status_code=409, detail="run_not_paused")
        return {"status": "PENDING"}

    @router.post("/runs/{run_id}/budget", status_code=202)
    def raise_run_budget(run_id: UUID, body: BudgetRaise) -> dict[str, Any]:
        """Give a run that reached its ceiling a new one, and continue it.

        Refuses a ceiling the run has already passed: that would stop it again
        on its very next call, which looks like the office ignoring the owner.
        """
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            status = run.status
        # Limits can be changed while the task is alive, not only once it has
        # already stopped: an owner watching a long task should be able to give
        # it room before it hits the wall.
        if status not in LIMIT_EDITABLE:
            raise HTTPException(status_code=409, detail="run_finished")
        budget = dict(d.repository.budget(run_id))
        spent = d.repository.usage(run_id)["spent"]
        asked = body.model_dump(exclude_none=True, exclude={"resume"})
        if not asked:
            raise HTTPException(status_code=422, detail="nothing_raised")
        if "max_tokens" in asked and asked["max_tokens"] <= int(spent["tokens"]):
            raise HTTPException(status_code=409, detail="below_spent_tokens")
        if (
            "max_cost_usd" in asked
            and spent["cost_usd"] is not None
            and asked["max_cost_usd"] <= float(spent["cost_usd"])
        ):
            raise HTTPException(status_code=409, detail="below_spent_cost")
        budget.update(asked)
        d.repository.set_budget(run_id, budget)
        d.repository.add_event(run_id, "budget_raised", {"budget": budget, "spent": spent})
        # Only a paused task needs picking up again; a running one simply
        # carries on under the new ceiling.
        resume = body.resume and status == RunStatus.PAUSED
        resumed = d.worker.resume_paused(run_id) if resume else False
        return {"budget": budget, "resumed": resumed}

    @router.post("/runs/{run_id}/memory/evaluate")
    async def evaluate_run_memory(
        run_id: UUID, owner: Owner, dry_run: bool = False
    ) -> dict[str, Any]:
        """Turn one finished run into candidate lessons for this project.

        Explicit only: nothing evaluates a run on its own, and `dry_run=true`
        shows exactly what would be sent to the evaluator without sending it.
        """
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            project_id = run.project_id
            project = found(session.get(Project, project_id), "Project")
            project_name = project.name
        artifacts: dict[str, dict[str, Any]] = {}
        for kind in ("research_report", "critique"):
            payload = latest_artifact_payload(d, run_id, kind)
            if payload is not None:
                artifacts[kind] = payload
        report, critique, usage, stopped = trace_of(d.repository, run_id, artifacts)
        try:
            candidates_found, probabilities, evaluator = await evaluate(
                report,
                critique,
                usage,
                stopped,
                project_name,
                build_judge(d.settings),
                dry_run=dry_run,
            )
        except NothingLearned:
            raise HTTPException(status_code=422, detail="nothing_learned") from None
        sent = projection(candidates_found)
        if dry_run:
            # Nothing is stored either: a preview changes nothing at all.
            return {"dry_run": True, "would_send": sent, "calls": len(sent), "stored": 0}
        with d.session_factory() as session, session.begin():
            added = record(session, project_id, run_id, candidates_found, probabilities, evaluator)
        return {
            "dry_run": False,
            "evaluated": len(candidates_found),
            "stored": added,
            "evaluator": evaluator,
            "owner": owner.name,
        }

    @router.post("/runs/{run_id}/entities/extract")
    async def extract_entities(run_id: UUID, owner: Owner, dry_run: bool = False) -> dict[str, Any]:
        """Propose what a finished task said about the subjects it was about.

        Taken from the run's own findings, sentence by sentence, and ranked by
        how durable each looks. Nothing enters a dossier until you keep it.
        """
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            project = found(session.get(Project, run.project_id), "Project")
            subjects = known_entities(session, project.venture_id)
        payload = latest_artifact_payload(d, run_id, "research_report") or {}
        candidates_found = facts_from(payload, subjects)
        if not candidates_found:
            raise HTTPException(status_code=422, detail="nothing_about_a_known_subject")
        would = [
            {"entity": c.entity, "fact": c.fact, "evidence": c.evidence} for c in candidates_found
        ]
        if dry_run:
            return {"dry_run": True, "would_send": would, "calls": len(would), "stored": 0}
        judge = build_judge(d.settings)
        probabilities: list[float] = []
        evaluator = None
        if judge is not None:
            try:
                probabilities = await judge.durable([(c.entity, c.fact) for c in candidates_found])
                evaluator = "jev-latest"
            except JudgmentUnavailable:
                probabilities = []
        with d.session_factory() as session, session.begin():
            subjects = known_entities(session, project.venture_id)
            added = record_facts(
                session, subjects, candidates_found, run_id, probabilities, evaluator
            )
        return {"proposed": len(candidates_found), "stored": added, "owner": owner.name}

    @router.get("/runs/{run_id}/answer")
    def run_answer(run_id: UUID) -> dict[str, Any]:
        """The run's answer and its versions, each with what changed."""
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
            answer = revisions.answer_view(session, run_id)
        if answer is None:
            raise HTTPException(status_code=404, detail="No answer yet")
        return answer

    @router.put("/runs/{run_id}/answer")
    def edit_answer(run_id: UUID, body: AnswerEdit) -> dict[str, Any]:
        """The owner's edit of the answer, saved as its next version."""
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        try:
            answer = revisions.save_edit(
                d.session_factory, d.repository, run_id, body.base_version, body.document
            )
        except revisions.StaleEdit as stale:
            raise HTTPException(status_code=409, detail=f"newer_version:{stale.args[0]}") from None
        except ValueError as invalid:
            raise HTTPException(status_code=422, detail=str(invalid)) from None
        if answer is None:
            raise HTTPException(status_code=404, detail="No answer yet")
        return answer

    @router.post("/runs/{run_id}/answer/rewrite")
    async def rewrite_answer_passage(run_id: UUID, body: AnswerRewrite) -> dict[str, Any]:
        """One passage rewritten by the lead, as a suggestion; a paid call made
        only when the owner presses Rewrite."""
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            topic_id, project_id = run.topic_id, run.project_id
        if topic_id is None:
            if project_id is None:
                raise HTTPException(status_code=409, detail="no_room")
            topic_id = _home(d, project_id)
        try:
            return await revisions.rewrite(
                d.session_factory,
                d.repository,
                d.settings,
                run_id,
                topic_id,
                body.passage,
                body.instruction,
                d.replies.complete,
            )
        except revisions.RewriteRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None

    @router.get("/runs/{run_id}/build/live")
    def build_live(run_id: UUID) -> dict[str, Any]:
        """The build as it is being written: plan, now, steps, comments, preview."""
        from app import builds

        secret = d.settings.owner_api_token.get_secret_value() if d.settings.owner_api_token else ""
        base = f"/previews/{run_id}/{builds.preview_token(secret, run_id)}" if secret else None
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            return live_view(session, run, d.settings.engineering_root, base)

    @router.post("/runs/{run_id}/build/notes", status_code=201)
    def build_note(run_id: UUID, body: BuildNote) -> dict[str, Any]:
        """Say something to the agent while it builds: at its next step,
        now, or pause it between steps and continue."""
        from app.live_build import live_view, pointed

        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            building = live_view(session, run, d.settings.engineering_root, None)["building"]
        if not building:
            raise HTTPException(status_code=409, detail="not_building")
        kind = {
            "next": "build_note",
            "now": "build_now",
            "pause": "build_pause",
            "continue": "build_continue",
        }[body.mode]
        text = (body.text or "").strip()
        aimed = pointed(body.pointed)
        if aimed and text:
            text = f"{text}\n{aimed}"
        return d.repository.add_control(run_id, kind, text or None)

    @router.get("/runs/{run_id}/build")
    def build_view(run_id: UUID) -> dict[str, Any]:
        """What the build holds: its files, its history, its last result."""
        builds, folder, state = _build_folder(d, run_id)
        report = dict(state.get("engineering") or {})
        secret = d.settings.owner_api_token.get_secret_value() if d.settings.owner_api_token else ""
        token = builds.preview_token(secret, run_id) if secret else ""
        every = builds.versions(folder)
        mine = next(
            (
                v
                for v in every
                if str(state.get("build_commit") or "") and v["commit"] == state.get("build_commit")
            ),
            None,
        )
        listed = builds.files_at(folder, mine["commit"]) if mine else builds.files(folder)
        has_page = any(f["path"] == "index.html" for f in listed)

        def page_of(commit: str) -> str | None:
            pages = {f["path"] for f in builds.files_at(folder, commit)}
            if "index.html" not in pages or not token:
                return None
            return f"/previews/{run_id}/{token}/v/{commit[:12]}/index.html"

        try:
            store: Any = open_secret_store(d.settings)
            tokens = {name: bool(_has_secret(d, store, s)) for name, s in hosting.TOKENS.items()}
        except SecretStoreError:
            tokens = {name: False for name in hosting.TOKENS}
        return {
            "folder": state.get("build_folder"),
            "published": hosting.published(d.settings, str(state.get("build_folder"))),
            "tokens": tokens,
            "files": listed,
            "history": builds.history(folder),
            "version": mine["number"] if mine else None,
            "commit": mine["commit"][:12] if mine else None,
            "versions": [
                {**v, "commit": v["commit"][:12], "preview": page_of(v["commit"])} for v in every
            ],
            "status": report.get("status"),
            "landed": bool(report.get("landed")),
            "tests": report.get("tests"),
            "errors": report.get("errors") or [],
            "summary": report.get("summary") or "",
            "preview": (
                page_of(mine["commit"])
                if mine
                else (f"/previews/{run_id}/{token}/index.html" if has_page and token else None)
            ),
        }

    @router.post("/runs/{run_id}/build/restore")
    def restore_build(run_id: UUID, body: BuildRestore) -> dict[str, Any]:
        """An earlier version brought back as a new one: nothing is lost."""
        builds, folder, _ = _build_folder(d, run_id)
        try:
            version = builds.version_of(folder, body.commit)
            if version.get("latest"):
                raise HTTPException(status_code=409, detail="already_latest")
            new = builds.restore(folder, version["commit"], run_id, int(version["number"]))
        except builds.BuildError as failed:
            raise HTTPException(status_code=409, detail=failed.code) from None
        d.repository.add_event(
            run_id, "build_restored", {"version": version["number"], "commit": new[:12]}
        )
        return {"restored": version["number"], "versions": len(builds.versions(folder))}

    @router.post("/runs/{run_id}/publish")
    async def publish_build(run_id: UUID, body: PublishRequest) -> dict[str, Any]:
        """Put the build's website online, by the owner's press."""
        import tempfile

        from app import hosting

        builds_module, folder, state = _build_folder(d, run_id)
        key = str(state.get("build_folder"))
        try:
            if body.version:
                # A chosen version, exported on its own.
                chosen = builds_module.version_of(folder, body.version)
                with tempfile.TemporaryDirectory() as scratch:
                    exported = builds_module.export_at(folder, chosen["commit"], Path(scratch))
                    files = hosting.site_files(hosting.site_root(exported))
            else:
                files = hosting.site_files(hosting.site_root(folder))
        except (hosting.HostingError, builds_module.BuildError) as missing:
            raise HTTPException(
                status_code=409, detail=getattr(missing, "code", "no_website")
            ) from None
        store = open_secret_store(d.settings)
        secret = hosting.TOKENS[body.target]
        try:
            if body.token is not None:
                store.put(secret, body.token.get_secret_value())
            token = store.get(secret)
        except SecretStoreError:
            raise HTTPException(status_code=409, detail="publish_token_missing") from None
        before = next(
            (row for row in hosting.published(d.settings, key) if row.get("target") == body.target),
            {},
        )
        try:
            async with httpx.AsyncClient(timeout=hosting.TIMEOUT, trust_env=False) as http:
                if body.target == "netlify":
                    done = await hosting.to_netlify(token, files, before.get("site_id"), http)
                else:
                    with d.session_factory() as session:
                        run = found(session.get(Run, run_id), "Run")
                        task = session.get(Task, run.root_task_id)
                        title = (task.title or task.objective) if task else "office-site"
                    done = await hosting.to_github(
                        token, body.name or title, files, before.get("repository"), http
                    )
        except hosting.HostingError as refused:
            raise HTTPException(status_code=409, detail=refused.code) from None
        rows = hosting.remember(d.settings, key, done)
        d.repository.add_event(
            run_id, "build_published", {"target": body.target, "url": done.get("url")}
        )
        return {"published": rows, "url": done.get("url")}

    @router.get("/runs/{run_id}/build/file")
    def build_file(run_id: UUID, path: str, v: str = "") -> dict[str, Any]:
        builds, folder, _ = _build_folder(d, run_id)
        try:
            if v:
                data = builds.read_at(folder, builds.version_of(folder, v)["commit"], path)
            else:
                data, _target = builds.read_file(folder, path)
        except builds.BuildError as missing:
            raise HTTPException(status_code=404, detail=missing.code) from None
        try:
            text = data[: builds.MAX_SHOWN_BYTES].decode("utf-8")
        except UnicodeDecodeError:
            return {"path": path, "bytes": len(data), "text": None}
        return {
            "path": path,
            "bytes": len(data),
            "text": text,
            "cut": len(data) > builds.MAX_SHOWN_BYTES,
        }

    @router.get("/runs/{run_id}/build.zip")
    def build_zip(run_id: UUID, v: str = "") -> Response:
        builds, folder, state = _build_folder(d, run_id)
        name = f"build-{str(run_id)[:8]}"
        try:
            if v:
                version = builds.version_of(folder, v)
                name = f"{name}-v{version['number']}"
                data = builds.zipped_at(folder, version["commit"], name)
            else:
                data = builds.zipped(folder, name)
        except builds.BuildError as large:
            raise HTTPException(status_code=413, detail=large.code) from None
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
        )

    @router.get("/runs/{run_id}/context")
    def run_context_view(run_id: UUID) -> dict[str, Any]:
        """What this task was told when it started, as stored with it."""
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            project = session.get(Project, run.project_id)
            return run_context_summary(run, project.name if project else "")

    @router.get("/runs/{run_id}/question")
    def run_question(run_id: UUID) -> dict[str, Any] | None:
        """What a paused run is waiting for the owner to decide, if anything."""
        with d.session_factory() as session:
            status = found(session.get(Run, run_id), "Run").status
        if status != RunStatus.PAUSED:
            return None
        stuck = d.repository.pending_stuck_ask(run_id)
        if stuck:
            return {"kind": "stuck", **stuck}
        ask = d.repository.pending_budget_ask(run_id)
        return {"kind": "budget", **ask} if ask else None

    @router.post("/runs/{run_id}/help", status_code=202)
    def help_run(run_id: UUID, body: RunHelp) -> dict[str, Any]:
        """The owner's answer to a stuck task (`RunActions.help`)."""
        try:
            return d.actions.help(run_id, body.action, body.text)
        except ActionRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.code) from None

    @router.get("/runs/{run_id}/events")
    def run_events(run_id: UUID, after: int = 0) -> list[dict[str, Any]]:
        """Durable progress events after a cursor, for live views that reconnect."""
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        return d.repository.events(run_id, after=max(after, 0))

    @router.get("/runs/{run_id}/usage")
    def run_usage(run_id: UUID) -> dict[str, Any]:
        """Budget and spend for a run, totalled by role. Unreported cost stays unknown."""
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        usage = d.repository.usage(run_id)
        # Runs created before budgets existed execute under the default one.
        usage["budget"] = usage["budget"] or d.settings.default_budget
        return usage

    @router.get("/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            detail = RunView.model_validate(run).model_dump()
            detail["labels"] = list(run.labels_json or [])
            detail["state"] = run.state_json or {}
            detail["config_snapshot"] = run.config_snapshot_json or {}
            task = session.get(Task, run.root_task_id)
            detail["objective"] = task.objective if task else ""
            # A short name to head the task; the objective stays beneath it.
            detail["title"] = title_of(task.title if task else None, detail["objective"])
            detail["approval_status"] = session.scalars(
                select(Approval.status)
                .where(Approval.run_id == run_id)
                .order_by(Approval.requested_at.desc())
                .limit(1)
            ).one_or_none()
            project = session.get(Project, run.project_id) if run.project_id else None
            detail["project_name"] = project.name if project else ""
            detail["business_line"] = business_line(session, project) if project else None
            detail["deliverable"] = (
                clean_deliverable(dict(run.deliverable_json)) if run.deliverable_json else None
            )
            detail["team_proposal"] = team_proposals.of_run(session, run_id)
            return detail

    @router.post("/runs/{run_id}/room", status_code=201)
    def post_in_the_room(run_id: UUID, body: TeamRoomPost) -> dict[str, Any]:
        """The owner's word in the task's room. It reaches the next huddle,
        and a post to a step is acted on as a challenge is: a finished step runs
        again with it in hand, one still to come reads it first."""
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            steps = {
                str(step.get("id"))
                for step in ((run.state_json or {}).get("plan") or {}).get("steps") or []
                if isinstance(step, dict)
            }
        if body.to_step is not None and body.to_step not in steps:
            raise HTTPException(status_code=422, detail="unknown_step")
        entry = {
            "key": f"owner-{uuid4().hex}",
            "kind": "owner",
            "author_role": "owner",
            "to_step": body.to_step,
            "text": body.text,
        }
        d.repository.add_team_post(run_id, entry)
        return next(
            (post for post in d.repository.team_posts(run_id) if post["key"] == entry["key"]), entry
        )

    @router.get("/runs/{run_id}/lineage")
    def get_run_lineage(run_id: UUID) -> dict[str, Any]:
        """How this task links to other work: what it built on and what built
        on it, what it was told and who was told it, what it taught."""
        with d.session_factory() as session:
            found_lineage = lineage(session, run_id)
        if found_lineage is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return found_lineage

    @router.get("/runs/{run_id}/flow")
    def get_run_flow(run_id: UUID) -> dict[str, Any]:
        """The task's workflow as one picture: lanes, steps, judgments, log."""
        with d.session_factory() as session:
            run = found(session.get(Run, run_id), "Run")
            on_call = advisor_on(session, d.settings, run.project_id) if run.project_id else False
            as_team = team_on(session, d.settings, run.project_id) if run.project_id else False
            return run_flow(session, run, d.settings.max_parallel_subtasks, on_call, as_team)

    @router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactView])
    def list_run_artifacts(run_id: UUID) -> list[Artifact]:
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
            return list(
                session.scalars(
                    select(Artifact)
                    .where(Artifact.run_id == run_id)
                    .order_by(Artifact.created_at, Artifact.version)
                )
            )

    @router.post("/runs/{run_id}/review", response_model=RunView)
    async def review_run(run_id: UUID, body: HumanReviewRequest) -> Any:
        """Act on a run that stopped for human attention.

        A run that ends at human review is otherwise a dead end: the reader can
        see why it stopped but cannot answer it. A revision records the
        person's instruction, starts a new bounded cycle, and resumes the run
        through the same edges an automated revision uses.

        Review is owner-authenticated like the surrounding task and run
        surfaces. Restarting analysis authorizes nothing consequential; the
        approval endpoints still require the same owner identity.
        """
        reviews = ReviewStore(d.session_factory)
        try:
            reviews.load(run_id)
        except ReviewRejected as rejected:
            code = 404 if rejected.reason == "unknown_run" else 409
            raise HTTPException(status_code=code, detail=rejected.reason) from None

        chosen_by = ""
        if body.action == "close":
            reviews.close(run_id, body.comment, d.settings.owner_name)
        else:
            # Accepting sets no target, which routes to synthesis and then the
            # approval gate. Revising sends the work back to a named node.
            target = None
            if body.action == "revise":
                target = body.revision_target
                if target is None:
                    target, chosen_by = await judged_target(d, body.comment)
            reopen(d, run_id, body.comment, target)
        note = body.comment.strip() or DEFAULT_NOTE[body.action]
        if chosen_by:
            note = f"{note} [revision target {chosen_by}]"
        record_review(d.repository, run_id, body.action, note, d.settings.owner_name)

        with d.session_factory() as session:
            return found(session.get(Run, run_id), "Run")

    @router.get("/runs/{run_id}/browser/{name}")
    def browser_shot(run_id: UUID, name: str) -> FileResponse:
        """A screenshot the office's browser took for this task."""
        if not re.fullmatch(r"\d{1,4}\.jpg", name):
            raise HTTPException(status_code=404, detail="Screenshot not found")
        path = d.settings.workspace_root / "browser" / str(run_id) / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Screenshot not found")
        return FileResponse(
            path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
        )

    @router.post("/runs/{run_id}/finish", response_model=RunView, status_code=202)
    def finish_run(run_id: UUID) -> Any:
        """Write the answer from the work done so far, with nothing more researched.

        For a task stopped at its limit or for review: closing it would keep
        nothing of what it found. The limit that was reached gets room for the
        one writing pass, which the owner asked for by pressing Finish.
        """
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        material = json.dumps(
            [
                latest_artifact_payload(d, run_id, kind)
                for kind in ("research_report", "strategy_proposal")
            ],
            default=str,
        )
        budget = dict(d.repository.budget(run_id))
        spent = d.repository.usage(run_id)["spent"]
        try:
            override = ReviewStore(d.session_factory).finish(run_id, d.settings.owner_name)
        except ReviewRejected as rejected:
            code = 404 if rejected.reason == "unknown_run" else 409
            raise HTTPException(status_code=code, detail=rejected.reason) from None
        roomier = room_to_finish(budget, spent, material, d.settings.llm_max_output_tokens)
        if roomier != budget:
            d.repository.set_budget(run_id, roomier)
        d.repository.add_event(run_id, "finish_requested", {"budget": roomier, "spent": spent})
        record_review(
            d.repository,
            run_id,
            "finish",
            "Finished with the work done so far.",
            d.settings.owner_name,
        )
        d.worker.resume_for_review(run_id, override)
        with d.session_factory() as session:
            return found(session.get(Run, run_id), "Run")

    @router.post("/runs/{run_id}/rework", response_model=RunView, status_code=202)
    async def rework_run(run_id: UUID, body: ReworkRequest) -> Any:
        """Redo part of a finished task, in the same task (`RunActions.rework`)."""
        try:
            await d.actions.rework(run_id, body.comment, body.revision_target)
        except ActionRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.code) from None
        with d.session_factory() as session:
            return found(session.get(Run, run_id), "Run")
