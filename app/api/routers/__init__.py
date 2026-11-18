"""The owner's API, assembled from one module per group of routes."""

from functools import partial

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, sessionmaker

from app.access import Reachable
from app.api.auth import owner_dependency
from app.api.calendar_routes import add_calendar_routes
from app.api.idea_routes import add_idea_routes
from app.api.routers.archive import add_archive_routes
from app.api.routers.coder import add_coder_routes
from app.api.routers.deps import RouterDeps
from app.api.routers.live import add_live_routes
from app.api.routers.model_calls import add_model_call_routes
from app.api.routers.office import add_office_routes
from app.api.routers.outputs import add_outputs_routes
from app.api.routers.projects import add_projects_routes
from app.api.routers.proposals import add_proposal_routes
from app.api.routers.queue import add_queue_routes
from app.api.routers.reach import add_reach_routes
from app.api.routers.routines import add_routines_routes
from app.api.routers.runs import add_runs_routes
from app.api.routers.shared import (
    _direct_from_topic,
    _pause_from_topic,
    _start_from_topic,
    deliver_queue,
    start_prep,
)
from app.api.routers.side_effects import add_side_effect_routes
from app.api.routers.team import add_team_routes
from app.api.routers.topics import add_topics_routes
from app.api.routers.verdicts import add_verdict_routes
from app.api.routers.workspaces import add_workspace_routes
from app.api.sessions import LoginCodes, LoginThrottle
from app.api.watch_routes import add_watch_routes
from app.calendars.store import Calendars
from app.config import Settings
from app.credentials import open_secret_store
from app.handoff import Handoff
from app.kernel import EventBus
from app.mcp import oauth as mcp_oauth
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.runner import RunRepository
from app.room_replies import RoomReplies
from app.run_actions import RunActions
from app.runtime.events import RunSettled
from app.runtime.worker import RunWorker


def build_router(
    session_factory: sessionmaker[Session],
    repository: RunRepository,
    worker: RunWorker,
    approvals: ApprovalStore,
    settings: Settings,
    sign_ins: mcp_oauth.Flows | None = None,
    codes: LoginCodes | None = None,
    reachable: Reachable | None = None,
    throttle: LoginThrottle | None = None,
    calendars: Calendars | None = None,
    events: EventBus | None = None,
) -> APIRouter:
    # Every data, run, artifact, review, and approval route is owner-scoped.
    # Liveness/health and the static browser shell remain public in ``main``.
    # Every data, run, artifact, review, and approval route is owner-scoped.
    # Liveness/health and the static browser shell remain public in ``main``.
    require_owner = owner_dependency(settings, session_factory, throttle)
    router = APIRouter(dependencies=[Depends(require_owner)])
    d = RouterDeps(
        session_factory=session_factory,
        repository=repository,
        worker=worker,
        approvals=approvals,
        settings=settings,
        codes=codes,
        reachable=reachable,
        throttle=throttle,
        calendars=calendars,
        require_owner=require_owner,
        # The owner's actions on a task, shared with Slack's buttons.
        actions=RunActions(session_factory, repository, worker, approvals, settings),
        calendar_service=calendars
        or Calendars(session_factory, lambda: open_secret_store(settings)),
        # Agents replying in project rooms. One per office, so "who is
        # replying" is known to every request.
        replies=RoomReplies(session_factory, settings, repository.store),
        # Sign-ins to tool servers in progress, redeemed once by the callback.
        sign_ins=sign_ins or mcp_oauth.Flows(lambda: open_secret_store(settings)),
        login_codes=codes or LoginCodes(),
    )
    d.handoff = Handoff(
        session_factory,
        settings,
        d.replies,
        partial(_start_from_topic, d),
        partial(_pause_from_topic, d),
        partial(_direct_from_topic, d),
    )
    if events is not None:
        # The owner's queued messages go to the thread when its work ends.
        events.subscribe(RunSettled, partial(deliver_queue, d))
    add_calendar_routes(router, session_factory, d.calendar_service, partial(start_prep, d))
    add_runs_routes(router, d)
    add_queue_routes(router, d)
    add_live_routes(router)
    add_side_effect_routes(router, d)
    add_model_call_routes(router, d)
    add_verdict_routes(router, d)
    add_proposal_routes(router, d)
    add_workspace_routes(router, d)
    add_outputs_routes(router, d)
    add_projects_routes(router, d)
    add_routines_routes(router, d)
    add_archive_routes(router, d)
    add_coder_routes(router, d)
    add_topics_routes(router, d)
    add_team_routes(router, d)
    add_reach_routes(router, d)
    add_office_routes(router, d)
    add_watch_routes(
        router,
        session_factory,
        settings,
        lambda agent, messages: d.replies.complete(agent, messages),
    )
    add_idea_routes(
        router,
        session_factory,
        d.handoff,
        require_owner,
        # Looked up at each call, so the owner's current backup applies.
        lambda agent, messages: d.replies.complete(agent, messages),
        settings,
    )
    return router
