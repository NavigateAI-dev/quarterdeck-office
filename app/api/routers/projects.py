"""Routes for projects, ventures, entities, templates and schedules."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import decisions, rooms, topics
from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routers.shared import (
    _home,
    _say,
    _topic_feed,
    added_event,
    save_template,
    source_view,
    template_view,
)
from app.api.routes import clean_role_doctrine, entity_view, found, known_skills, venture_view
from app.api.schemas import (
    EntityCreate,
    EntityUpdate,
    MemoryDecision,
    ObjectiveCreate,
    ObjectiveUpdate,
    ProjectCreate,
    ProjectDelete,
    ProjectDeliverable,
    ProjectSettingsUpdate,
    ProjectUpdate,
    ProjectVenture,
    ProjectView,
    RoomAction,
    RoomPost,
    RunView,
    ScheduleCreate,
    SchedulePost,
    SkillImport,
    SourceView,
    TemplateCreate,
    VentureCreate,
    VentureUpdate,
)
from app.db.models import (
    ChannelDestination,
    ChannelInstallation,
    Entity,
    EntityFact,
    KnowledgeSource,
    Objective,
    Project,
    ProjectMemory,
    ProjectSource,
    RoomMessage,
    Run,
    Schedule,
    TaskTemplate,
    Topic,
    UsageEvent,
    Venture,
)
from app.deliverable import clean as clean_deliverable
from app.entity_store import candidates as fact_candidates
from app.entity_store import decide as decide_entity_fact
from app.entity_store import view as fact_view
from app.judgments import build_judge
from app.mcp import store as mcp_store
from app.memory.portable import NoProvenance, read_skill, to_skill
from app.memory.store import CANDIDATE, decide, record
from app.memory.store import candidates as memory_candidates
from app.memory.store import view as memory_view
from app.objectives import progress
from app.office_settings import MonthlyLimitReached, OfficePaused, effective
from app.orchestrator.runner import UnknownProject
from app.project_context import context_summary
from app.project_data import ProjectDataError, delete_project, export_project, remove_project_files
from app.project_settings import budget_for, settings_view
from app.project_settings import save as save_project_settings
from app.project_settings import spent_this_month as spent_in_project
from app.run_actions import INSTRUCTABLE
from app.run_start import start_run
from app.scheduler import rule_of
from app.scheduling import Recurrence, next_occurrence, preview
from app.venture_routing import OWNER, line_suggestion, sort_projects


def schedule_view(d: RouterDeps, row: Schedule) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": str(row.id),
        "template_id": str(row.template_id),
        "timezone": row.timezone,
        "frequency": row.frequency,
        "local_time": row.local_time,
        "weekday": row.weekday,
        "enabled": row.enabled,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "preview": preview(rule_of(row), now) if row.enabled else [],
        "last_run_id": str(row.last_run_id) if row.last_run_id else None,
        "last_outcome": row.last_outcome,
        "disabled_reason": row.disabled_reason,
        "post_to": str(row.post_to_id) if row.post_to_id else None,
    }


def checked_post_to(d: RouterDeps, session: Session, destination_id: UUID | None) -> UUID | None:
    """A channel the office can post a report to, or None for the office only."""
    if destination_id is None:
        return None
    destination = session.get(ChannelDestination, destination_id)
    installation = (
        session.get(ChannelInstallation, destination.installation_id) if destination else None
    )
    if destination is None or installation is None or installation.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="channel_not_available")
    return destination_id


def add_projects_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/projects/{project_id}/sources", response_model=list[SourceView])
    def project_sources(project_id: UUID) -> list[dict[str, Any]]:
        with d.session_factory() as session:
            found(session.get(Project, project_id), "Project")
            rows = session.scalars(
                select(KnowledgeSource)
                .join(ProjectSource, ProjectSource.source_id == KnowledgeSource.id)
                .where(ProjectSource.project_id == project_id)
                .order_by(KnowledgeSource.name)
            )
            return [source_view(d, row) for row in rows]

    @router.put("/projects/{project_id}/sources/{source_id}", status_code=204)
    def grant_source(project_id: UUID, source_id: UUID) -> None:
        with d.session_factory() as session, session.begin():
            found(session.get(Project, project_id), "Project")
            found(session.get(KnowledgeSource, source_id), "Source")
            exists = session.scalar(
                select(ProjectSource.id).where(
                    ProjectSource.project_id == project_id, ProjectSource.source_id == source_id
                )
            )
            if exists is None:
                session.add(ProjectSource(project_id=project_id, source_id=source_id))

    @router.delete("/projects/{project_id}/sources/{source_id}", status_code=204)
    def revoke_source(project_id: UUID, source_id: UUID) -> None:
        with d.session_factory() as session, session.begin():
            session.execute(
                delete(ProjectSource).where(
                    ProjectSource.project_id == project_id, ProjectSource.source_id == source_id
                )
            )

    @router.get("/projects/{project_id}/export")
    def export_project_archive(project_id: UUID) -> Response:
        with d.session_factory() as session:
            project = found(session.get(Project, project_id), "Project")
            data = export_project(session, project, d.repository.store)
        return Response(
            content=data,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="project-{project_id}.tar.gz"'},
        )

    @router.delete("/projects/{project_id}")
    def delete_project_route(project_id: UUID, body: ProjectDelete) -> dict[str, Any]:
        """Delete a project and its history. Requires the exact project name."""
        try:
            with d.session_factory() as session, session.begin():
                project = found(session.get(Project, project_id), "Project")
                result = delete_project(session, project, body.confirm, d.repository.store)
        except ProjectDataError as error:
            raise HTTPException(status_code=409, detail=error.code) from None
        remove_project_files(d.repository.store.root, project_id)
        return result

    @router.post("/projects", response_model=ProjectView, status_code=201)
    def create_project(body: ProjectCreate) -> Project:
        with d.session_factory() as session, session.begin():
            project = Project(name=body.name, description=body.description)
            session.add(project)
            session.flush()
            session.refresh(project)
            return project

    @router.put("/projects/{project_id}", response_model=ProjectView)
    def update_project(project_id: UUID, body: ProjectUpdate) -> Project:
        """Rename a project, or say again what it is about."""
        with d.session_factory() as session, session.begin():
            project = found(session.get(Project, project_id), "Project")
            if body.name is not None:
                project.name = body.name
            if body.description is not None:
                project.description = body.description.strip()
            session.flush()
            session.refresh(project)
            return project

    @router.get("/projects", response_model=list[ProjectView])
    def list_projects() -> list[Project]:
        with d.session_factory() as session:
            return list(session.scalars(select(Project).order_by(Project.created_at.desc())))

    @router.get("/projects/{project_id}/settings")
    def project_settings_view(project_id: UUID) -> dict[str, Any]:
        """What this project sets for itself, and what it inherits."""
        with d.session_factory() as session:
            project = found(session.get(Project, project_id), "Project")
            office = effective(session, d.settings)
            return {
                "project": settings_view(project),
                "office": {
                    "monthly_cost_limit_usd": office["monthly_cost_limit_usd"],
                    "run_max_tokens": office["run_max_tokens"],
                    "run_max_cost_usd": office["run_max_cost_usd"],
                    "run_max_minutes": office["run_max_minutes"],
                    "role_models": office["role_models"],
                    "advisor_on": office["advisor_on"],
                    "team_on": office["team_on"],
                },
                "in_force": budget_for(session, d.settings, project_id),
                "spent_this_month_usd": round(spent_in_project(session, project_id), 4),
            }

    @router.put("/projects/{project_id}/settings")
    def update_project_settings(project_id: UUID, body: ProjectSettingsUpdate) -> dict[str, Any]:
        """Set this project's own limits, models and channel. Null returns one to the office's."""
        with d.session_factory() as session, session.begin():
            project = found(session.get(Project, project_id), "Project")
            changes = body.model_dump(exclude_unset=True)
            if changes.get("notify_destination_id"):
                found(
                    session.get(ChannelDestination, changes["notify_destination_id"]),
                    "Destination",
                )
            save_project_settings(project, changes)
            session.flush()
        return project_settings_view(project_id)

    @router.get("/projects/{project_id}/summary")
    def project_summary(project_id: UUID) -> dict[str, Any]:
        """One project's work: its tasks by state, its spend, its knowledge."""
        with d.session_factory() as session:
            project = found(session.get(Project, project_id), "Project")
            rows = session.execute(
                select(Run.status, func.count(Run.id))
                .where(Run.project_id == project_id)
                .group_by(Run.status)
            ).all()
            spent = round(spent_in_project(session, project_id), 4)
            tokens = int(
                session.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                func.coalesce(UsageEvent.prompt_tokens, 0)
                                + func.coalesce(UsageEvent.completion_tokens, 0)
                            ),
                            0,
                        )
                    ).where(UsageEvent.project_id == project_id)
                )
                or 0
            )
            sources = [
                {"id": str(row.id), "name": row.name, "documents": row.document_count}
                for row in session.scalars(
                    select(KnowledgeSource)
                    .join(
                        ProjectSource,
                        ProjectSource.source_id == KnowledgeSource.id,
                    )
                    .where(ProjectSource.project_id == project_id)
                )
            ]
            venture = session.get(Venture, project.venture_id) if project.venture_id else None
            return {
                "id": str(project.id),
                "name": project.name,
                "description": project.description,
                "status": project.status,
                "venture_id": str(project.venture_id) if project.venture_id else None,
                "deliverable": clean_deliverable(dict(project.deliverable_json or {})),
                "venture_name": venture.name if venture else None,
                "venture_set_by": project.venture_set_by,
                "venture_suggestion": line_suggestion(session, project),
                "tasks": {str(status): int(count) for status, count in rows},
                "spent_this_month_usd": spent,
                "tokens": tokens,
                "sources": sources,
            }

    @router.get("/projects/{project_id}", response_model=ProjectView)
    def get_project(project_id: UUID) -> Project:
        with d.session_factory() as session:
            return found(session.get(Project, project_id), "Project")

    @router.post("/templates", status_code=201)
    def create_template(body: TemplateCreate) -> dict[str, Any]:
        return save_template(
            d,
            {
                "name": body.name,
                "project_id": body.project_id,
                "objective": body.objective,
                "budget_json": body.budget.model_dump() if body.budget else {},
            },
        )

    @router.get("/templates")
    def list_templates() -> list[dict[str, Any]]:
        with d.session_factory() as session:
            rows = session.scalars(select(TaskTemplate).order_by(TaskTemplate.name))
            return [template_view(d, row) for row in rows]

    @router.post("/templates/{template_id}/run", response_model=RunView, status_code=202)
    def run_template(template_id: UUID) -> Run:
        with d.session_factory() as session:
            template = found(session.get(TaskTemplate, template_id), "Template")
            project_id, objective, budget = (
                template.project_id,
                template.objective,
                dict(template.budget_json or {}),
            )
        if project_id is None:
            raise HTTPException(status_code=409, detail="template_project_deleted")
        try:
            return start_run(
                d.session_factory,
                d.repository,
                d.worker,
                d.settings,
                project_id,
                objective,
                budget or None,
            )
        except OfficePaused:
            raise HTTPException(status_code=409, detail="office_paused") from None
        except MonthlyLimitReached:
            raise HTTPException(status_code=409, detail="monthly_limit_reached") from None

    @router.delete("/templates/{template_id}", status_code=204)
    def delete_template(template_id: UUID) -> None:
        with d.session_factory() as session, session.begin():
            template = found(session.get(TaskTemplate, template_id), "Template")
            session.execute(delete(Schedule).where(Schedule.template_id == template_id))
            session.delete(template)

    @router.post("/schedules", status_code=201)
    def create_schedule(body: ScheduleCreate) -> dict[str, Any]:
        """Run a template at a local time. Scheduled work needs the office awake."""
        try:
            rule = Recurrence(
                timezone=body.timezone,
                frequency=body.frequency,
                local_time=body.local_time,
                weekday=body.weekday,
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_schedule") from None
        with d.session_factory() as session, session.begin():
            template = found(session.get(TaskTemplate, body.template_id), "Template")
            if template.project_id is None:
                raise HTTPException(status_code=409, detail="template_project_deleted")
            row = Schedule(
                template_id=body.template_id,
                timezone=rule.timezone,
                frequency=rule.frequency,
                local_time=rule.local_time,
                weekday=rule.weekday,
                enabled=True,
                next_run_at=next_occurrence(rule, datetime.now(UTC)),
                post_to_id=checked_post_to(d, session, body.post_to),
            )
            session.add(row)
            session.flush()
            return schedule_view(d, row)

    @router.get("/schedules")
    def list_schedules() -> list[dict[str, Any]]:
        with d.session_factory() as session:
            return [
                schedule_view(d, row)
                for row in session.scalars(select(Schedule).where(Schedule.prompt.is_(None)))
            ]

    @router.get("/schedules/{schedule_id}")
    def get_schedule(schedule_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            return schedule_view(d, found(session.get(Schedule, schedule_id), "Schedule"))

    @router.put("/schedules/{schedule_id}/post")
    def set_schedule_post(schedule_id: UUID, body: SchedulePost) -> dict[str, Any]:
        """Post each run's result to a chat channel, or stop posting it."""
        with d.session_factory() as session, session.begin():
            row = found(session.get(Schedule, schedule_id), "Schedule")
            row.post_to_id = checked_post_to(d, session, body.post_to)
            session.flush()
            return schedule_view(d, row)

    @router.post("/schedules/{schedule_id}/{action}")
    def toggle_schedule(schedule_id: UUID, action: str) -> dict[str, Any]:
        if action not in ("enable", "disable"):
            raise HTTPException(status_code=404, detail="Not Found")
        with d.session_factory() as session, session.begin():
            row = found(session.get(Schedule, schedule_id), "Schedule")
            if action == "disable":
                row.enabled = False
                row.disabled_reason = "disabled_by_owner"
            else:
                template = session.get(TaskTemplate, row.template_id)
                if template is None or template.project_id is None:
                    raise HTTPException(status_code=409, detail="template_project_deleted")
                row.enabled = True
                row.disabled_reason = None
                # Missed occurrences are not caught up after re-enabling.
                row.next_run_at = next_occurrence(rule_of(row), datetime.now(UTC))
            session.flush()
            return schedule_view(d, row)

    @router.delete("/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: UUID) -> None:
        with d.session_factory() as session, session.begin():
            session.delete(found(session.get(Schedule, schedule_id), "Schedule"))

    @router.get("/ventures")
    def list_ventures(include_archived: bool = False) -> list[dict[str, Any]]:
        """The business lines this office works for."""
        with d.session_factory() as session:
            statement = select(Venture).order_by(Venture.created_at)
            if not include_archived:
                statement = statement.where(Venture.status == "ACTIVE")
            return [venture_view(row, session) for row in session.scalars(statement)]

    @router.post("/ventures", status_code=201)
    def create_venture(body: VentureCreate) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            venture = Venture(
                name=body.name,
                description=body.description,
                doctrine=body.doctrine,
                role_doctrine_json=clean_role_doctrine(body.role_doctrine),
                skill_ids_json=known_skills(session, body.skills or []),
            )
            session.add(venture)
            try:
                session.flush()
            except IntegrityError:
                raise HTTPException(status_code=409, detail="venture_name_taken") from None
            return venture_view(venture, session)

    @router.put("/ventures/{venture_id}")
    def update_venture(venture_id: UUID, body: VentureUpdate) -> dict[str, Any]:
        """Change what a business line is, or how work is judged inside it.

        New doctrine reaches the next task. Work already finished keeps the
        context it actually ran under.
        """
        with d.session_factory() as session, session.begin():
            venture = found(session.get(Venture, venture_id), "Venture")
            for field in ("name", "description", "doctrine", "status"):
                value = getattr(body, field)
                if value is not None:
                    setattr(venture, field, value)
            if body.role_doctrine is not None:
                venture.role_doctrine_json = clean_role_doctrine(body.role_doctrine)
            if body.skills is not None:
                venture.skill_ids_json = known_skills(session, body.skills)
            session.flush()
            return venture_view(venture, session)

    @router.get("/entities")
    def list_entities(venture_id: UUID | None = None) -> list[dict[str, Any]]:
        """The subjects the work keeps returning to."""
        with d.session_factory() as session:
            rows = session.scalars(
                select(Entity).where(Entity.status == "ACTIVE").order_by(Entity.name)
            )
            chosen = [
                row for row in rows if venture_id is None or row.venture_id in (None, venture_id)
            ]
            return [entity_view(row, session) for row in chosen]

    @router.post("/entities", status_code=201)
    def create_entity(body: EntityCreate) -> dict[str, Any]:
        """Name a subject worth keeping a dossier on.

        The owner names these: they know their domain in a way no extraction
        does, and a wrong subject would collect wrong facts.
        """
        with d.session_factory() as session, session.begin():
            if body.venture_id is not None:
                found(session.get(Venture, body.venture_id), "Venture")
            entity = Entity(
                name=body.name,
                kind=body.kind,
                aliases_json=[a.strip() for a in body.aliases if a.strip()],
                venture_id=body.venture_id,
            )
            session.add(entity)
            try:
                session.flush()
            except IntegrityError:
                raise HTTPException(status_code=409, detail="entity_name_taken") from None
            return entity_view(entity, session)

    @router.put("/entities/{entity_id}")
    def update_entity(entity_id: UUID, body: EntityUpdate) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            entity = found(session.get(Entity, entity_id), "Entity")
            if body.name is not None:
                entity.name = body.name
            if body.kind is not None:
                entity.kind = body.kind
            if body.status is not None:
                entity.status = body.status
            if body.aliases is not None:
                entity.aliases_json = [a.strip() for a in body.aliases if a.strip()]
            session.flush()
            return entity_view(entity, session)

    @router.get("/entities/{entity_id}/facts")
    def entity_facts(entity_id: UUID, status: str = "CANDIDATE") -> list[dict[str, Any]]:
        """A subject's dossier: what is waiting for you, or what you kept."""
        with d.session_factory() as session:
            found(session.get(Entity, entity_id), "Entity")
            if status.upper() == "APPROVED":
                rows = session.scalars(
                    select(EntityFact)
                    .where(EntityFact.entity_id == entity_id, EntityFact.status == "APPROVED")
                    .order_by(EntityFact.decided_at.desc().nullslast())
                )
                return [fact_view(row) for row in rows]
            return fact_candidates(session, entity_id)

    @router.post("/facts/{fact_id}/decision")
    def decide_fact(fact_id: UUID, body: MemoryDecision, owner: Owner) -> dict[str, Any]:
        """Keep a line in a subject's dossier, or discard it."""
        with d.session_factory() as session, session.begin():
            if not decide_entity_fact(session, fact_id, body.approved, owner.name):
                raise HTTPException(status_code=409, detail="already_decided")
        return {"status": "APPROVED" if body.approved else "REJECTED"}

    @router.get("/ventures/{venture_id}/objectives")
    def list_objectives(venture_id: UUID, open_only: bool = False) -> list[dict[str, Any]]:
        """What this business line is working towards, and what the work came to."""
        with d.session_factory() as session:
            found(session.get(Venture, venture_id), "Venture")
            rows = session.scalars(
                select(Objective)
                .where(Objective.venture_id == venture_id)
                .order_by(Objective.status, Objective.ends_on.asc().nullslast())
            )
            chosen = [row for row in rows if not open_only or row.status == "OPEN"]
            return [progress(session, row, d.repository.store) for row in chosen]

    @router.post("/ventures/{venture_id}/objectives", status_code=201)
    def create_objective(venture_id: UUID, body: ObjectiveCreate) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            found(session.get(Venture, venture_id), "Venture")
            objective = Objective(
                venture_id=venture_id,
                title=body.title,
                measure=body.measure,
                starts_on=body.starts_on,
                ends_on=body.ends_on,
            )
            session.add(objective)
            session.flush()
            return progress(session, objective, d.repository.store)

    @router.put("/objectives/{objective_id}")
    def update_objective(objective_id: UUID, body: ObjectiveUpdate) -> dict[str, Any]:
        """Change an objective, or close it as met or missed.

        Closing is the owner's word. Work is evidence for it, never a verdict:
        nothing here closes an objective because tasks finished.
        """
        with d.session_factory() as session, session.begin():
            objective = found(session.get(Objective, objective_id), "Objective")
            for field in ("title", "measure", "starts_on", "ends_on", "status"):
                value = getattr(body, field)
                if value is not None:
                    setattr(objective, field, value)
            session.flush()
            return progress(session, objective, d.repository.store)

    @router.post("/projects/sort-into-lines")
    async def sort_into_lines() -> dict[str, Any]:
        """Put every unassigned project under the business line it belongs to.

        Jev suggests; the office applies a confident answer and puts an unsure
        one to the owner. A project it judges to belong to no line is left
        alone rather than pushed into the nearest one.
        """
        with d.session_factory() as session, session.begin():
            return await sort_projects(session, build_judge(d.settings))

    @router.put("/projects/{project_id}/venture")
    def set_project_venture(project_id: UUID, body: ProjectVenture) -> dict[str, Any]:
        """Put a project's work under a business line, or under none."""
        with d.session_factory() as session, session.begin():
            project = found(session.get(Project, project_id), "Project")
            if body.venture_id is not None:
                found(session.get(Venture, body.venture_id), "Venture")
            project.venture_id = body.venture_id
            # The owner's choice, "none" included, is never moved by the office.
            project.venture_set_by = OWNER
            # Its discussions belong where it does.
            session.execute(
                update(Topic)
                .where(Topic.project_id == project.id)
                .values(venture_id=body.venture_id)
            )
            session.flush()
            return {"project_id": str(project.id), "venture_id": str(body.venture_id or "") or None}

    @router.put("/projects/{project_id}/mcp/{server_id}", status_code=204)
    def grant_mcp_server(project_id: UUID, server_id: UUID, owner: Owner) -> None:
        """Let this project's tasks use a server's reviewed tools."""
        with d.session_factory() as session, session.begin():
            found(session.get(Project, project_id), "Project")
            try:
                mcp_store.grant(session, project_id, server_id)
            except LookupError as missing:
                raise HTTPException(status_code=404, detail=str(missing.args[0])) from None

    @router.delete("/projects/{project_id}/mcp/{server_id}", status_code=204)
    def revoke_mcp_server(project_id: UUID, server_id: UUID, owner: Owner) -> None:
        """Take it away. Its tools stop being offered to this project at once."""
        with d.session_factory() as session, session.begin():
            mcp_store.revoke(session, project_id, server_id)

    @router.get("/projects/{project_id}/room")
    def project_room(
        project_id: UUID, after_event: int = 0, after_message: int = 0, limit: int = rooms.PAGE
    ) -> dict[str, Any]:
        """The project's first topic after two cursors (what its room was)."""
        return _topic_feed(d, _home(d, project_id), after_event, after_message, limit)

    @router.post("/projects/{project_id}/room", status_code=201)
    def say_in_room(
        project_id: UUID, body: RoomPost, owner: Owner, background: BackgroundTasks
    ) -> dict[str, Any]:
        """Said in the project's first topic (what its room was)."""
        return _say(d, _home(d, project_id), body, owner, background)

    @router.get("/projects/{project_id}/topic")
    def project_home_topic(project_id: UUID) -> dict[str, Any]:
        """The project's first topic, made when first asked for."""
        topic_id = _home(d, project_id)
        with d.session_factory() as session:
            return topics.view(session, found(session.get(Topic, topic_id), "Topic"))

    @router.get("/ventures/{venture_id}/space")
    def business_space(venture_id: UUID) -> dict[str, Any]:
        """A business line as one place: decisions with their history, goals,
        discussions, projects and materials."""
        with d.session_factory() as session:
            venture = found(session.get(Venture, venture_id), "Business line")
            return decisions.space(session, venture)

    @router.get("/projects/{project_id}/documents")
    def project_documents(project_id: UUID) -> list[dict[str, Any]]:
        """What this project's tasks came to: each one's answer, newest first."""
        with d.session_factory() as session:
            found(session.get(Project, project_id), "Project")
            return rooms.documents(session, project_id)

    @router.post(
        "/projects/{project_id}/room/messages/{message_id}/actions/{index}", status_code=201
    )
    def press_room_action(
        project_id: UUID, message_id: int, index: int, body: RoomAction, owner: Owner
    ) -> dict[str, Any]:
        """Carry out a button the room offered: start a proposed task, or send
        the owner's words to a running one. Only buttons the room itself put on
        that message can be pressed, and each only once.
        """
        with d.session_factory() as session, session.begin():
            # Locked, so two presses of one button start one task.
            row = session.scalar(
                select(RoomMessage)
                .where(RoomMessage.id == message_id, RoomMessage.project_id == project_id)
                .with_for_update()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Message not found")
            actions = [dict(action) for action in row.actions_json or []]
            if not 0 <= index < len(actions):
                raise HTTPException(status_code=404, detail="Action not found")
            action = actions[index]
            if action.get("kind") == "start_task":
                if action.get("run_id"):
                    raise HTTPException(status_code=409, detail="already_done")
                objective = body.objective or str(action.get("objective") or "")
                try:
                    started = start_run(
                        d.session_factory, d.repository, d.worker, d.settings, project_id, objective
                    )
                except UnknownProject:
                    raise HTTPException(status_code=404, detail="Project not found") from None
                except OfficePaused:
                    raise HTTPException(status_code=409, detail="office_paused") from None
                except MonthlyLimitReached:
                    raise HTTPException(status_code=409, detail="monthly_limit_reached") from None
                action.update(run_id=str(started.id), objective=objective, done_by=owner.name)
            elif action.get("kind") == "send_as_direction":
                if action.get("control_id"):
                    raise HTTPException(status_code=409, detail="already_done")
                run = session.get(Run, UUID(str(action.get("run_id"))))
                if run is None or run.status not in INSTRUCTABLE:
                    raise HTTPException(status_code=409, detail="run_not_accepting_instructions")
                control = d.repository.add_control(
                    run.id, "instruction", row.body, body.target_node
                )
                action.update(control_id=control["id"], done_by=owner.name)
            elif action.get("kind") == "add_event":
                added_event(d, action, body, owner.name)
            else:
                raise HTTPException(status_code=404, detail="Action not found")
            actions[index] = action
            row.actions_json = actions
        return {"action": action}

    @router.get("/projects/{project_id}/mcp")
    def project_mcp_servers(project_id: UUID) -> dict[str, Any]:
        """The servers this project may use, and what its tasks are told."""
        with d.session_factory() as session:
            found(session.get(Project, project_id), "Project")
            return {
                "servers": [
                    mcp_store.view(row) for row in mcp_store.granted_to(session, project_id)
                ],
                "told": mcp_store.told_about(session, project_id),
            }

    @router.put("/projects/{project_id}/deliverable")
    def set_project_deliverable(project_id: UUID, body: ProjectDeliverable) -> dict[str, Any]:
        """What this project's tasks should come to by default."""
        with d.session_factory() as session, session.begin():
            project = found(session.get(Project, project_id), "Project")
            project.deliverable_json = clean_deliverable(body.model_dump())
            session.flush()
            return {"project_id": str(project.id), "deliverable": project.deliverable_json}

    @router.get("/projects/{project_id}/context")
    def project_context_view(project_id: UUID) -> dict[str, Any]:
        """What a task in this project will be told before it starts."""
        with d.session_factory() as session:
            project = found(session.get(Project, project_id), "Project")
            return context_summary(session, project, d.settings)

    @router.get("/projects/{project_id}/memory")
    def project_memory(project_id: UUID, status: str = "CANDIDATE") -> list[dict[str, Any]]:
        """What this project has learned: candidates to review, or what is approved."""
        with d.session_factory() as session:
            found(session.get(Project, project_id), "Project")
            if status.upper() == "APPROVED":
                rows = session.scalars(
                    select(ProjectMemory)
                    .where(
                        ProjectMemory.project_id == project_id,
                        ProjectMemory.status == "APPROVED",
                    )
                    .order_by(ProjectMemory.decided_at.desc().nullslast())
                )
                return [memory_view(row) for row in rows]
            return memory_candidates(session, project_id)

    @router.get("/projects/{project_id}/memory/skills")
    def export_memory_skills(project_id: UUID) -> dict[str, Any]:
        """This project's approved lessons as Agent Skills another harness reads.

        Files, not a download: the owner decides where they land, and a skill
        the office wrote is plain text that can be read before it is trusted.
        """
        with d.session_factory() as session:
            found(session.get(Project, project_id), "Project")
            rows = session.scalars(
                select(ProjectMemory).where(
                    ProjectMemory.project_id == project_id,
                    ProjectMemory.status == "APPROVED",
                )
            )
            office = d.settings.office_name or "Quarterdeck"
            return {"format": 1, "files": [to_skill(row, office) for row in rows]}

    @router.post("/projects/{project_id}/memory/skills")
    def import_memory_skill(project_id: UUID, body: SkillImport, owner: Owner) -> dict[str, Any]:
        """A SKILL.md from another harness, as a candidate for review.

        It arrives as a candidate and never as an approved lesson: what another
        harness believed is a proposal here, whoever learned it.
        """
        try:
            read = read_skill(body.text)
        except NoProvenance as missing:
            raise HTTPException(status_code=422, detail=f"no_provenance:{missing}") from missing
        with d.session_factory() as session, session.begin():
            found(session.get(Project, project_id), "Project")
            added = record(
                session,
                project_id,
                None,
                [read["candidate"]],
                evaluator=read["evaluator"],
            )
        return {
            "status": CANDIDATE,
            "added": added,
            "learned_by": read["learned_by"],
            "learned_at": read["learned_at"],
            "lesson": read["candidate"].lesson,
        }

    @router.post("/memory/{memory_id}/decision")
    def decide_memory(memory_id: UUID, body: MemoryDecision, owner: Owner) -> dict[str, Any]:
        """Approve a lesson into this project's memory, or reject it.

        Only an approved lesson is ever told to an agent.
        """
        with d.session_factory() as session, session.begin():
            if not decide(session, memory_id, body.approved, owner.name, body.scope):
                raise HTTPException(status_code=409, detail="already_decided")
        return {"status": "APPROVED" if body.approved else "REJECTED"}
