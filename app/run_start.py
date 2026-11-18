"""Start a run: the one path shared by the API, templates and the scheduler."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app import run_state, widgets
from app.attachments import attach
from app.calendars.store import agent_view
from app.config import Settings
from app.db.models import Objective, Project, Run
from app.deliverable import DEFAULT as DEFAULT_DELIVERABLE
from app.deliverable import asked_for
from app.deliverable import clean as clean_deliverable
from app.entity_store import dossiers_for
from app.follow_up import carried_context
from app.mcp.store import told_about
from app.office_settings import effective
from app.orchestrator.graph import ANALYSIS_WORKFLOW, LEAD_WORKFLOW
from app.orchestrator.runner import RunRepository, UnknownProject
from app.project_context import build_run_context
from app.project_settings import budget_for, check_limits
from app.runtime.job import Job
from app.runtime.worker import RunWorker

# The task box asks for this: the lead, or the full team when the request needs its steps.
AUTO_WORKFLOW = "auto"


def answering_workflow(
    session: Session,
    settings: Settings,
    project_id: UUID,
    deliverable: dict[str, Any] | None,
    use_browser: bool,
) -> str:
    """The lead for a request it can answer on screen; the full team otherwise.

    A formatted deliverable (slides, a document) or the office's browser needs
    the full team's steps, so those keep it.
    """
    if use_browser:
        return ANALYSIS_WORKFLOW
    project = session.get(Project, project_id)
    wanted = clean_deliverable(
        deliverable or dict((project.deliverable_json if project else None) or {})
    )
    return LEAD_WORKFLOW if wanted == DEFAULT_DELIVERABLE else ANALYSIS_WORKFLOW


def start_run(
    session_factory: sessionmaker[Session],
    repository: RunRepository,
    worker: RunWorker,
    settings: Settings,
    project_id: UUID,
    objective: str,
    budget: dict[str, Any] | None = None,
    workflow: str = ANALYSIS_WORKFLOW,
    origin: dict[str, Any] | None = None,
    serves_objective_id: UUID | None = None,
    deliverable: dict[str, Any] | None = None,
    continues_run_id: UUID | None = None,
    title: str | None = None,
    topic_id: UUID | None = None,
    origin_message_id: int | None = None,
    discussion: list[str] | None = None,
    handed_to: str | None = None,
    use_browser: bool = False,
    attachments: list[UUID] | None = None,
) -> Run:
    """Record the run durably, then hand it to the worker. Raises UnknownProject."""
    with session_factory() as session:
        # The limits are checked before anything durable is created, so a
        # refused start leaves no half-made run behind.
        check_limits(session, settings, project_id)
        chosen = budget or budget_for(session, settings, project_id)
        if workflow == AUTO_WORKFLOW:
            workflow = answering_workflow(session, settings, project_id, deliverable, use_browser)
    dispatch = repository.create(project_id, objective, chosen, workflow, origin, title=title)
    with session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise UnknownProject(str(project_id))
        if attachments:
            # Granted to the task's project before its context is read, so it
            # is among the documents the task may use.
            attached = attach(session, attachments, project.id)
            session.commit()
        context = build_run_context(session, project, settings)
        if attachments:
            context["attached"] = [
                f"{item['name']} ({item['documents']} documents)" for item in attached
            ]
        # Only the subjects this task is about: a dossier is a reminder, not an
        # encyclopaedia of everything the office has ever researched.
        context["dossiers"] = dossiers_for(session, project, objective)
        # The tools this project was granted, as far as the owner reviewed them.
        context["tools"] = told_about(session, project_id)
        # The task's own choice, or what this project asks for by default.
        wanted = clean_deliverable(deliverable or dict(project.deliverable_json or {}))
        context["deliverable"] = asked_for(wanted)
        # The office's own browser, when the owner turned it on.
        context["browser"] = bool(effective(session, settings)["browser_on"]) and (
            settings.browser_exchange is not None
        )
        # Asked for by the owner: the plan uses the browser, not only may.
        context["browse_asked"] = bool(use_browser and context["browser"])
        # The owner's next days, as much as they let agents see.
        # Only while the calendar is on the wall and agents may read it.
        schedule = (
            agent_view(session, datetime.now(UTC)) if widgets.reads(session, "calendar") else ""
        )
        if schedule:
            context["schedule"] = schedule
        # The other widgets on the owner's wall that agents may read.
        wall = widgets.agent_context(session, datetime.now(UTC), "task", skip=("calendar",))
        if wall:
            context["wall"] = wall
        values: dict[str, Any] = {"deliverable_json": wanted}
        if topic_id is not None:
            # Handed off in a discussion: it reports back there, and is
            # told what was being said when it was asked for.
            values["topic_id"] = topic_id
            values["origin_message_id"] = origin_message_id
            # Started by the owner's press in a discussion: it asks before it
            # spends more on revising its own answer.
            context["from_discussion"] = True
            if discussion:
                context["discussion"] = list(discussion)
        if continues_run_id is not None:
            # What the earlier task established, told to this one as what was
            # found last time rather than as settled fact.
            # Any project: building on a task elsewhere is the owner's choice,
            # and the office is one place of work, not a set of walled rooms.
            parent = session.get(Run, continues_run_id)
            if parent is not None:
                context["continues"] = carried_context(session, repository.store, parent)
                values["continues_run_id"] = continues_run_id
        session.execute(update(Run).where(Run.id == dispatch.run_id).values(**values))
        session.commit()
        if serves_objective_id is not None:
            # Named here rather than inferred: the office does not guess which
            # objective a piece of work was for.
            served = session.get(Objective, serves_objective_id)
            if served is not None and served.venture_id == project.venture_id:
                context["serves"] = served.title
                session.execute(
                    update(Run)
                    .where(Run.id == dispatch.run_id)
                    .values(objective_id=serves_objective_id)
                )
                session.commit()
        # Stored with the run from the start: the task page shows what it was
        # told, and a run recovered before its first step keeps its context.
        state: dict[str, Any] = {"context": context}
        if handed_to:
            # The colleague the owner asked to do it, named on its card.
            state["handed_to"] = handed_to
        started = session.get(Run, dispatch.run_id)
        assert started is not None  # noqa: S101 - created above
        run_state.write(session, started, run_state.REPLACE, state, "start")
        session.commit()
        run = session.get(Run, dispatch.run_id)
    worker.submit(Job(dispatch=dispatch, objective=objective, context=context))
    assert run is not None  # noqa: S101 - created above
    return run
